import Foundation
import CryptoKit

public struct Capture: Codable, Sendable {
    public var id = UUID().uuidString
    public var deviceId: String
    public var sessionId: String
    public var receivedAt: Double
    public var source: String
    public var rawBase64: String
    public var sampleAt: Double?
    public var hrBpm: Int?
    public var rrMs: [Double]?
    public var intervalWords: [UInt16]?
    public var intervalStatus: String?
    public var acceleration: [Double]?
    public var batteryPercent: Double?
    public var quality: String?
    public var decoderVersion = "whoop4-v2"

    public init(deviceId: String, sessionId: String, source: String, raw: Data,
                receivedAt: Double = Date().timeIntervalSince1970) {
        self.deviceId = deviceId; self.sessionId = sessionId
        self.source = source; self.rawBase64 = raw.base64EncodedString()
        self.receivedAt = receivedAt
    }
    public mutating func useHistoricalIdentity() {
        let bytes = Data((deviceId + ":" + rawBase64).utf8)
        id = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    }
}

public enum WireError: Error { case truncated, invalid, checksum }

public enum WhoopWire {
    public static func crc8(_ bytes: [UInt8]) -> UInt8 {
        bytes.reduce(UInt8(0)) { running, byte in
            var value = running ^ byte
            for _ in 0..<8 { value = value & 0x80 != 0 ? (value &<< 1) ^ 7 : value &<< 1 }
            return value
        }
    }
    public static func crc32(_ bytes: [UInt8]) -> UInt32 {
        var value: UInt32 = 0xffffffff
        for byte in bytes {
            value ^= UInt32(byte)
            for _ in 0..<8 { value = value & 1 != 0 ? (value >> 1) ^ 0xedb88320 : value >> 1 }
        }
        return value ^ 0xffffffff
    }
    public static func littleEndian(_ value: UInt32) -> [UInt8] {
        (0..<4).map { UInt8(truncatingIfNeeded: value >> ($0 * 8)) }
    }
    public static func u16(_ b: [UInt8], _ i: Int) -> UInt16 {
        UInt16(b[i]) | UInt16(b[i + 1]) << 8
    }
    public static func u32(_ b: [UInt8], _ i: Int) -> UInt32 {
        UInt32(u16(b, i)) | UInt32(u16(b, i + 2)) << 16
    }
    public static func command(_ command: UInt8, sequence: UInt8, payload: [UInt8] = []) -> Data {
        let body: [UInt8] = [0x23, sequence, command] + payload
        let length = body.count + 4
        let header = [UInt8(length & 255), UInt8(length >> 8)]
        return Data([0xaa] + header + [crc8(header)] + body + littleEndian(crc32(body)))
    }

    /// Standard Bluetooth 2A37: interval units are 1/1024 second, not milliseconds.
    public static func heartRate(_ data: Data) throws -> (Int, [Double], Bool?) {
        let (hr, words, contact) = try heartRateWords(data)
        return (hr, words.map { Double($0) * 1000 / 1024 }, contact)
    }

    /// Preserve words independently of the BLE unit convention: this strap's units are unvalidated.
    public static func heartRateWords(_ data: Data) throws -> (Int, [UInt16], Bool?) {
        let b = [UInt8](data)
        guard b.count >= 2 else { throw WireError.truncated }
        let flags = b[0]; var offset = 1
        let hr: Int
        if flags & 1 != 0 {
            guard b.count >= 3 else { throw WireError.truncated }
            hr = Int(u16(b, 1)); offset = 3
        } else { hr = Int(b[1]); offset = 2 }
        if flags & 8 != 0 { offset += 2 }
        guard offset <= b.count else { throw WireError.truncated }
        var rr: [UInt16] = []
        if flags & 16 != 0 {
            guard (b.count - offset) % 2 == 0 else { throw WireError.truncated }
            while offset < b.count {
                rr.append(u16(b, offset)); offset += 2
            }
        }
        return (hr, rr, flags & 4 != 0 ? flags & 2 != 0 : nil)
    }

    public static func customBattery(_ frame: Frame) -> Double? {
        guard frame.type == 0x24, frame.command == 26, frame.payload.count >= 4,
              frame.payload[1] == 1 else { return nil }
        let percent = Double(u16(frame.payload, 2)) / 10
        return percent <= 100 ? percent : nil
    }

    /// WHOOP 4 compact live frame. Timestamp seconds + 1/32768-second fraction.
    public static func liveDeviceTime(_ frame: Frame) -> Double? {
        guard frame.type == 0x28, frame.version == 2, frame.raw.count == 28 else { return nil }
        let b = [UInt8](frame.raw)
        let fraction = u16(b, 10)
        guard fraction < 32768 else { return nil }
        return Double(u32(b, 6)) + Double(fraction) / 32768
    }

    /// Observed WHOOP 4 GET_CLOCK response: echoed command sequence, success,
    /// uint32 seconds, uint16 ticks, and five reserved bytes. Require exact shape.
    public static func clockResponse(_ frame: Frame) -> (sequence: UInt8, time: Double)? {
        guard frame.type == 0x24, frame.command == 11, frame.raw.count == 24,
              frame.payload.count == 13, frame.payload[1] == 1 else { return nil }
        let fraction = u16(frame.payload, 6)
        guard fraction < 32768 else { return nil }
        return (frame.payload[0], Double(u32(frame.payload, 2)) + Double(fraction) / 32768)
    }

    /// Decode only the documented v24 93-byte WHOOP 4 record. Preserve other versions raw.
    public static func historical(_ frame: Frame, capture: inout Capture) {
        guard frame.type == 0x2f, frame.version == 24, frame.payload.count == 93 else {
            capture.quality = "unsupported_layout"; return
        }
        let b = frame.payload
        let fraction = u16(b, 8)
        let timestamp = Double(u32(b, 4)) + Double(fraction) / 32768
        guard timestamp >= 1577836800, timestamp <= capture.receivedAt + 86400,
              fraction < 32768, b[15] <= 4 else { capture.quality = "invalid_history"; return }
        capture.sampleAt = timestamp
        capture.hrBpm = Int(b[14])
        capture.intervalWords = (0..<Int(b[15])).map { u16(b, 16 + $0 * 2) }
        capture.intervalStatus = "units_and_order_unverified"
        let axes = [33, 37, 41].map { Double(Float(bitPattern: u32(b, $0))) }
        if axes.allSatisfy({ $0.isFinite && abs($0) < 32 }) { capture.acceleration = axes }
        // Historical interval overlap/order must be compared against live 2A37 on hardware.
        capture.quality = "historical_intervals_unverified"
    }
}

public struct Frame: Sendable {
    public let raw: Data
    public let type: UInt8
    public let version: UInt8
    public let command: UInt8
    public let payload: [UInt8]
    public init(_ raw: Data) throws {
        let b = [UInt8](raw)
        guard b.count >= 11, b[0] == 0xaa,
              Int(WhoopWire.u16(b, 1)) + 4 == b.count else { throw WireError.invalid }
        guard WhoopWire.crc8(Array(b[1...2])) == b[3],
              WhoopWire.crc32(Array(b[4..<(b.count - 4)])) == WhoopWire.u32(b, b.count - 4)
        else { throw WireError.checksum }
        self.raw = raw; type = b[4]; version = b[5]; command = b[6]
        payload = Array(b[7..<(b.count - 4)])
    }
}

/// Routes notifications independently per characteristic. Callers preserve all original bytes.
public struct NotificationDecoder {
    // These three channels carry the CRC8/CRC32 envelope. 0007 uses another
    // format on the tested strap; preserve it raw without feeding it to this parser.
    private static let framedChannels = Set([3, 4, 5].map {
        "6108000\($0)-8d6d-82b8-614a-1c8cb0f8dcc6"
    })
    private var streams: [String: Reassembler] = [:]
    public private(set) var discardedBytes = 0
    public init() {}
    public mutating func append(_ bytes: Data, characteristic: String) -> [Data] {
        let key = characteristic.lowercased()
        guard Self.framedChannels.contains(key) else { return [] }
        var stream = streams[key] ?? Reassembler()
        let previous = stream.discardedBytes
        let frames = stream.append(bytes)
        discardedBytes += stream.discardedBytes - previous
        streams[key] = stream
        return frames
    }
}

/// One reassembler per characteristic. Keep notification bytes separately for forensic replay.
public struct Reassembler {
    private var buffer: [UInt8] = []
    public private(set) var discardedBytes = 0
    public init() {}
    public mutating func append(_ bytes: Data) -> [Data] {
        buffer.append(contentsOf: bytes); var result: [Data] = []
        while buffer.count >= 4 {
            guard buffer[0] == 0xaa else { buffer.removeFirst(); discardedBytes += 1; continue }
            let size = Int(WhoopWire.u16(buffer, 1)) + 4
            guard size >= 11, size <= 16384, WhoopWire.crc8(Array(buffer[1...2])) == buffer[3]
            else { buffer.removeFirst(); discardedBytes += 1; continue }
            guard buffer.count >= size else { break }
            result.append(Data(buffer.prefix(size))); buffer.removeFirst(size)
        }
        return result
    }
}
