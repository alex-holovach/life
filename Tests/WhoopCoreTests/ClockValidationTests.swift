import XCTest
@testable import WhoopCore

final class ClockValidationTests: XCTestCase {
    func testClockResponseRequiresExactSchemaAndEchoesRequestSequence() throws {
        func response(status: UInt8 = 1, ticks: UInt16 = 16384, extra: Bool = false) throws -> Frame {
            let payload: [UInt8] = [42,status] + WhoopWire.littleEndian(1_800_000_000)
                + [UInt8(truncatingIfNeeded: ticks),UInt8(ticks >> 8)] + Array(repeating: 0,count: extra ? 6 : 5)
            let body: [UInt8] = [0x24,7,11] + payload
            let header: [UInt8] = [UInt8(body.count+4),0]
            return try Frame(Data([0xaa] + header + [WhoopWire.crc8(header)] + body + WhoopWire.littleEndian(WhoopWire.crc32(body))))
        }
        let decoded = WhoopWire.clockResponse(try response())
        XCTAssertEqual(decoded?.sequence,42)
        XCTAssertEqual(decoded?.time,1_800_000_000.5)
        XCTAssertNil(WhoopWire.clockResponse(try response(status: 0)))
        XCTAssertNil(WhoopWire.clockResponse(try response(ticks: 32768)))
        XCTAssertNil(WhoopWire.clockResponse(try response(extra: true)))
    }
    func testDelayedLiveFramesNeverAuthorizeClockCorrection() {
        var clock = ClockValidation()
        for _ in 0..<10 { XCTAssertEqual(clock.observeLive(device: 100, received: 110), .none) }
        XCTAssertEqual(clock.observeLive(device: 109, received: 110), .none)
        XCTAssertEqual(clock.observeLive(device: 110, received: 111), .verified)
        XCTAssertEqual(clock.observeLive(device: 100, received: 120), .none)
        XCTAssertTrue(clock.verified)
    }
    func testCorrectionsNeedTwoMatchedFastRoundTripsWithConsistentOffsets() {
        var clock = ClockValidation()
        clock.request(sequence: 3, wall: 100, uptime: 20)
        XCTAssertEqual(clock.response(sequence: 2, device: 90, wall: 100.5, uptime: 20.5), .none)
        XCTAssertEqual(clock.response(sequence: 3, device: 90.2, wall: 100.5, uptime: 20.5), .none)
        clock.request(sequence: 4, wall: 110, uptime: 30)
        XCTAssertEqual(clock.response(sequence: 4, device: 100.2, wall: 110.5, uptime: 30.5), .correct)
        clock.request(sequence: 5, wall: 111, uptime: 31)
        XCTAssertEqual(clock.response(sequence: 5, device: 111.2, wall: 111.5, uptime: 31.5), .verified)
    }
    func testLateResponsesAndWallClockChangesCannotSetTheStrapClock() {
        var clock = ClockValidation()
        for i in 0..<3 {
            clock.request(sequence: UInt8(i), wall: 100, uptime: 20)
            XCTAssertEqual(clock.response(sequence: UInt8(i), device: 90, wall: 110, uptime: 30), .none)
        }
        clock.request(sequence: 4, wall: 100, uptime: 20)
        XCTAssertEqual(clock.response(sequence: 4, device: 90, wall: 200, uptime: 20.5), .none)
        XCTAssertFalse(clock.verified)
    }
}
