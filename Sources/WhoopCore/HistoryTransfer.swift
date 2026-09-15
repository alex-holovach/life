import Foundation

/// Retry a damaged transfer on a fresh connection without advancing its cursor.
/// Unknown layouts and rejected ACKs stay blocked for protocol investigation.
public struct HistoryRecovery {
    public private(set) var retryAt: TimeInterval?
    private var attempts = 0
    private var furthestRecord: Double?
    public init() {}
    @discardableResult
    public mutating func failed(_ reason: String, at now: TimeInterval) -> Bool {
        guard ["history_timeout", "history_ack_timeout", "record_gap", "record_without_start",
               "framing_mismatch", "invalid_checksum"].contains(reason) else {
            retryAt = nil; return false
        }
        if retryAt == nil {
            retryAt = now + min(300, 5 * pow(2, Double(min(attempts, 6))))
            attempts += 1
        }
        return true
    }
    public func isDue(at now: TimeInterval) -> Bool { retryAt.map { now >= $0 } ?? false }
    public mutating func connected() { retryAt = nil }
    public mutating func progress(through timestamp: Double) {
        guard timestamp.isFinite, furthestRecord.map({ timestamp > $0 }) ?? true else { return }
        furthestRecord = timestamp; attempts = 0
    }
    public mutating func completed() { self = Self() }
}

/// ACK is a persistent cursor advance. All raw frames and decoded records must
/// commit before returning it. The caller archives every notification fragment too.
public struct HistoryTransfer {
    public enum Action: Equatable { case none, ack([UInt8]), complete, abort(String) }
    public struct Gap: Equatable {
        public let version: UInt8
        public let expected: UInt32
        public let received: UInt32
    }
    public private(set) var lastGap: Gap?
    public private(set) var running = false
    public private(set) var records = 0
    private var activity: TimeInterval = 0
    private var inBatch = false
    private var lastCounters: [UInt8: UInt32] = [:]
    private var lastToken: [UInt8]?
    private var ackAt: TimeInterval = 0
    private var repeats = 0

    public init() {}
    public mutating func begin(at now: TimeInterval) {
        self = Self(); running = true; activity = now
    }
    public mutating func invalidate() { running = false; inBatch = false }
    public mutating func check(at now: TimeInterval) -> Action {
        guard running else { return .none }
        // A large backlog can take longer than ten minutes while making progress.
        // Interrupt only idle transfers; restarting mid-batch races queued records.
        if now - activity >= 30 { return abort("history_timeout") }
        return .none
    }
    public mutating func accept(_ frame: Frame, at now: TimeInterval, persist: () throws -> Void) throws -> Action {
        do { try persist() } catch { invalidate(); throw error }
        guard running else { return .none }
        if frame.type == 0x2f {
            activity = now
            guard inBatch else { return abort("record_without_start") }
            // This strap interleaves 104-byte v24 and 84-byte v25 frames, with
            // independent counters. v25 is archived verbatim, never decoded as HR.
            let knownSize = (frame.version == 24 && frame.payload.count == 93)
                || (frame.version == 25 && frame.payload.count == 73)
            guard knownSize else { return abort("unknown_history_layout") }
            let counter = WhoopWire.u32(frame.payload, 0)
            if let previous = lastCounters[frame.version], counter != previous &+ 1 {
                lastGap = Gap(version: frame.version, expected: previous &+ 1, received: counter)
                return abort("record_gap")
            }
            lastCounters[frame.version] = counter; records += 1
        } else if frame.type == 0x31 {
            activity = now
            switch frame.command {
            case 1: inBatch = true; lastCounters = [:]
            case 2:
                guard inBatch else { return .none }
                guard frame.payload.count == 21 else { return abort("unknown_end_marker") }
                let token = Array(frame.payload[10..<18])
                if token == lastToken {
                    guard now - ackAt >= 5 else { return .none }
                    repeats += 1
                    guard repeats <= 3 else { return abort("history_ack_timeout") }
                } else { repeats = 0 }
                lastToken = token; ackAt = now
                return .ack([1] + token)
            case 3: invalidate(); return .complete
            default: return abort("unknown_history_marker")
            }
        }
        return .none
    }
    private mutating func abort(_ reason: String) -> Action { invalidate(); return .abort(reason) }
}
