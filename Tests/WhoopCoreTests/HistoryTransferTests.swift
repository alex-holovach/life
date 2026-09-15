import XCTest
@testable import WhoopCore

final class HistoryTransferTests: XCTestCase {
    private func frame(_ type: UInt8, _ version: UInt8, _ command: UInt8, _ payload: [UInt8]) throws -> Frame {
        let body = [type, version, command] + payload
        let size = body.count + 4
        let length = [UInt8(size & 255), UInt8(size >> 8)]
        return try Frame(Data([0xaa] + length + [WhoopWire.crc8(length)] + body + WhoopWire.littleEndian(WhoopWire.crc32(body))))
    }
    private func record(_ counter: UInt32) throws -> Frame {
        var data = [UInt8](repeating: 0, count: 93)
        data.replaceSubrange(0..<4, with: WhoopWire.littleEndian(counter))
        data.replaceSubrange(4..<8, with: WhoopWire.littleEndian(1_789_360_000))
        data[14] = 72
        return try frame(47, 24, 5, data)
    }
    private func end() throws -> Frame {
        var payload = [UInt8](repeating: 0, count: 21)
        payload.replaceSubrange(10..<18, with: [5, 0, 0, 0, 24, 0, 0, 0])
        return try frame(49, 12, 2, payload)
    }
    func testAcknowledgementRequiresDurableRecordsAndMarker() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: url.path + suffix) } }
        let store = try CaptureStore(url: url)
        var transfer = HistoryTransfer(); transfer.begin(at: 0)
        let records = [try frame(49, 10, 1, [UInt8](repeating: 0, count: 37)), try record(40), try record(41), try end()]
        for (i, frame) in records.enumerated() {
            let action = try transfer.accept(frame, at: Double(i)) {
                var row = Capture(deviceId: "test", sessionId: "test", source: "history", raw: frame.raw)
                row.useHistoricalIdentity(); try store.insert(row)
            }
            if i == 3 {
                XCTAssertEqual(action, .ack([1, 5, 0, 0, 0, 24, 0, 0, 0]))
                XCTAssertEqual(try CaptureStore(url: url).pending().count, 4, "The complete burst must survive reopening before ACK")
            } else { XCTAssertEqual(action, .none) }
        }
        XCTAssertEqual(transfer.records, 2)
        XCTAssertEqual(try transfer.accept(end(), at: 4) {}, .none, "Suppress rapid duplicate ACKs")
        XCTAssertEqual(try transfer.accept(end(), at: 9) {}, .ack([1, 5, 0, 0, 0, 24, 0, 0, 0]))
    }
    func testStorageFailureOrRecordGapPreventsCursorAdvance() throws {
        enum Disk: Error { case full }
        var t = HistoryTransfer(); t.begin(at: 0)
        _ = try t.accept(frame(49, 1, 1, []), at: 0) {}
        XCTAssertThrowsError(try t.accept(record(1), at: 1) { throw Disk.full })
        XCTAssertEqual(try t.accept(end(), at: 2) {}, .none)
        t.begin(at: 3)
        _ = try t.accept(frame(49, 1, 1, []), at: 3) {}
        _ = try t.accept(record(10), at: 4) {}
        XCTAssertEqual(try t.accept(record(12), at: 5) {}, .abort("record_gap"))
        XCTAssertEqual(t.lastGap?.version,24)
        XCTAssertEqual(t.lastGap?.expected,11)
        XCTAssertEqual(t.lastGap?.received,12)
        XCTAssertEqual(try t.accept(end(), at: 6) {}, .none)
    }
    func testCoalescedNotificationIsDurableBeforeAnyAcknowledgement() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appendingPathComponent("captures.sqlite")
        let store = try CaptureStore(url: url)
        let frames = [try frame(49, 1, 1, []), try record(1), try record(2), try end()]
        var rows = [Capture(deviceId: "test", sessionId: "test", source: "raw",
                            raw: frames.reduce(Data()) { $0 + $1.raw })]
        for frame in frames {
            rows.append(Capture(deviceId: "test", sessionId: "test", source: "wire_frame", raw: frame.raw))
            if frame.type == 0x2f {
                var row = Capture(deviceId: "test", sessionId: "test", source: "history", raw: frame.raw)
                row.useHistoricalIdentity(); WhoopWire.historical(frame, capture: &row); rows.append(row)
            }
        }
        try store.insert(rows)
        var transfer = HistoryTransfer(); transfer.begin(at: 0)
        var acknowledgements = 0
        for frame in frames {
            if case .ack = try transfer.accept(frame, at: 1, persist: {}) {
                acknowledgements += 1
                XCTAssertEqual(try CaptureStore(url: url).pending().map(\.id), rows.map(\.id))
            }
        }
        XCTAssertEqual(acknowledgements, 1)
        XCTAssertEqual(transfer.records, 2)
    }
    func testTruncatedMarkerCorruptionTimeoutAndCompletion() throws {
        var t = HistoryTransfer(); t.begin(at: 0)
        // Late markers from a previous session must never advance our new cursor.
        XCTAssertEqual(try t.accept(end(), at: 1) {}, .none)
        _ = try t.accept(frame(49, 1, 1, []), at: 2) {}
        XCTAssertEqual(try t.accept(frame(49, 2, 2, [0]), at: 3) {}, .abort("unknown_end_marker"))
        t.begin(at: 4); t.invalidate()
        XCTAssertEqual(try t.accept(end(), at: 5) {}, .none)
        t.begin(at: 6)
        XCTAssertEqual(t.check(at: 36), .abort("history_timeout"))
        t.begin(at: 40)
        XCTAssertEqual(try t.accept(frame(49, 3, 3, []), at: 41) {}, .complete)
        XCTAssertFalse(t.running)
    }

    func testInterleavedHistoryHasIndependentCountersAndRemainsLossless() throws {
        func companion(_ counter: UInt32, version: UInt8 = 25) throws -> Frame {
            var data = [UInt8](repeating: 0, count: 73)
            data.replaceSubrange(0..<4, with: WhoopWire.littleEndian(counter))
            return try frame(47, version, 0, data)
        }
        var t = HistoryTransfer(); t.begin(at: 0)
        _ = try t.accept(frame(49, 1, 1, []), at: 0) {}
        var persisted = 0
        for f in [try record(31_805_060), try companion(20_061), try record(31_805_061), try companion(20_062)] {
            XCTAssertEqual(try t.accept(f, at: 1) { persisted += 1 }, .none)
        }
        XCTAssertEqual(persisted, 4)
        XCTAssertEqual(try t.accept(end(), at: 2) {}, .ack([1, 5, 0, 0, 0, 24, 0, 0, 0]))
        XCTAssertEqual(try t.accept(companion(20_064), at: 3) {}, .abort("record_gap"))
        t.begin(at: 4)
        _ = try t.accept(frame(49, 1, 1, []), at: 4) {}
        XCTAssertEqual(try t.accept(companion(1, version: 26), at: 5) {}, .abort("unknown_history_layout"))
        var capture = Capture(deviceId: "test", sessionId: "test", source: "history", raw: try companion(1).raw)
        WhoopWire.historical(try companion(1), capture: &capture)
        XCTAssertNil(capture.hrBpm)
        XCTAssertEqual(capture.quality, "unsupported_layout")
    }

    func testActiveBackfillContinuesBeyondTenMinutes() throws {
        var transfer = HistoryTransfer(); transfer.begin(at: 0)
        _ = try transfer.accept(frame(49, 1, 1, []), at: 0) {}
        for counter in 0...700 {
            _ = try transfer.accept(record(UInt32(counter)), at: Double(counter)) {}
            XCTAssertEqual(transfer.check(at: Double(counter)), .none)
        }
        XCTAssertEqual(try transfer.accept(end(), at: 701) {}, .ack([1, 5, 0, 0, 0, 24, 0, 0, 0]))
        XCTAssertEqual(transfer.check(at: 731), .abort("history_timeout"))
    }

    func testOpticalHistoryCounterWrapsAt16BitsWithoutHidingMissingRecords() throws {
        func optical(_ counter: UInt32) throws -> Frame {
            var data = [UInt8](repeating: 0, count: 73)
            data.replaceSubrange(0..<4, with: WhoopWire.littleEndian(counter))
            return try frame(47, 25, 0, data)
        }
        var transfer = HistoryTransfer(); transfer.begin(at: 0)
        _ = try transfer.accept(frame(49, 1, 1, []), at: 0) {}
        var saved = 0
        for counter: UInt32 in [65534, 65535, 0, 1] {
            XCTAssertEqual(try transfer.accept(optical(counter), at: 1) { saved += 1 }, .none)
        }
        XCTAssertEqual(saved, 4)
        XCTAssertEqual(try transfer.accept(end(), at: 2) {}, .ack([1, 5, 0, 0, 0, 24, 0, 0, 0]))

        transfer.begin(at: 3)
        _ = try transfer.accept(frame(49, 1, 1, []), at: 3) {}
        _ = try transfer.accept(optical(65535), at: 4) {}
        XCTAssertEqual(try transfer.accept(optical(1), at: 5) {}, .abort("record_gap"))
        XCTAssertEqual(transfer.lastGap?.expected, 0)

        transfer.begin(at: 6)
        _ = try transfer.accept(frame(49, 1, 1, []), at: 6) {}
        XCTAssertEqual(try transfer.accept(optical(65536), at: 7) {}, .abort("unknown_history_layout"))
    }

    func testHistoryRecoveryRetriesDroppedPacketsWithBoundedBackoff() throws {
        var recovery = HistoryRecovery()
        XCTAssertTrue(recovery.failed("record_gap", at: 10))
        XCTAssertFalse(recovery.isDue(at: 14))
        XCTAssertTrue(recovery.isDue(at: 15))
        // More callbacks from the damaged session must not postpone recovery.
        XCTAssertTrue(recovery.failed("record_without_start", at: 14))
        XCTAssertEqual(recovery.retryAt, 15)
        recovery.connected()
        XCTAssertFalse(recovery.isDue(at: 15))
        XCTAssertTrue(recovery.failed("history_timeout", at: 20))
        XCTAssertEqual(recovery.retryAt, 30)
        for _ in 0..<20 { recovery.connected(); _ = recovery.failed("record_gap", at: 100) }
        XCTAssertEqual(recovery.retryAt, 400)
        recovery.completed()
        XCTAssertTrue(recovery.failed("framing_mismatch", at: 500))
        XCTAssertEqual(recovery.retryAt, 505)
        XCTAssertFalse(recovery.failed("unknown_history_layout", at: 501))
        XCTAssertNil(recovery.retryAt)
        XCTAssertFalse(recovery.failed("ack_rejected", at: 502))
    }
    func testBackoffResetsOnForwardHistoryProgressButNotRepeatedRecords() {
        var recovery = HistoryRecovery()
        recovery.progress(through: 100)
        _ = recovery.failed("record_gap", at: 0)
        recovery.connected(); recovery.progress(through: 99)
        _ = recovery.failed("record_gap", at: 10)
        XCTAssertEqual(recovery.retryAt, 20)
        recovery.connected(); recovery.progress(through: 100)
        _ = recovery.failed("history_timeout", at: 30)
        XCTAssertEqual(recovery.retryAt, 50)
        recovery.connected(); recovery.progress(through: 101)
        _ = recovery.failed("history_timeout", at: 60)
        XCTAssertEqual(recovery.retryAt, 65)
    }
}
