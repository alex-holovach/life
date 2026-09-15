import XCTest
@testable import WhoopCore

final class CoreTests: XCTestCase {
    func testNotificationRoutingPreservesOpaqueChannelAndChecksFramedStreams() throws {
        var decoder = NotificationDecoder()
        let suffix = "-8D6D-82B8-614A-1C8CB0F8DCC6"
        // Observed 0007 prefix uses a different envelope; its complete payload stays archived raw.
        let opaque = Data([0x08, 0x02, 0xa6, 0x02, 0x01, 0x03, 0x01, 0x0a])
        XCTAssertTrue(decoder.append(opaque, characteristic: "61080007" + suffix).isEmpty)
        let frame = WhoopWire.command(26, sequence: 1, payload: [0])
        XCTAssertTrue(decoder.append(frame.prefix(5), characteristic: "61080003" + suffix).isEmpty)
        XCTAssertEqual(decoder.append(frame, characteristic: "61080004" + suffix), [frame])
        XCTAssertEqual(decoder.append(frame.dropFirst(5), characteristic: "61080003" + suffix), [frame])
        XCTAssertEqual(decoder.append(frame, characteristic: "61080005" + suffix), [frame])
        XCTAssertEqual(decoder.discardedBytes, 0)
        _ = decoder.append(Data([0, 1, 2, 3]) + frame, characteristic: "61080005" + suffix)
        XCTAssertEqual(decoder.discardedBytes, 4, "Real frame corruption must still raise a warning")
    }
    func testPublishedHeartRatePacketAndOptionalFields() throws {
        // whoop4-ble README capture: 103e1904 => 62 BPM, 1049/1024 seconds.
        let (hr, rr, _) = try WhoopWire.heartRate(Data([0x10, 0x3e, 0x19, 0x04]))
        XCTAssertEqual(hr, 62); XCTAssertEqual(rr[0], 1024.4140625)
        let expanded = try WhoopWire.heartRate(Data([0x19, 0x2c, 0x01, 0, 0, 0, 4, 0, 2]))
        XCTAssertEqual(expanded.0, 300); XCTAssertEqual(expanded.1, [1000, 500])
        XCTAssertThrowsError(try WhoopWire.heartRate(Data([0x10, 60, 1])))
        XCTAssertThrowsError(try WhoopWire.heartRate(Data([0x09, 60, 0])))
    }
    func testCRCCheckVectors() {
        XCTAssertEqual(WhoopWire.crc32(Array("123456789".utf8)), 0xcbf43926)
        XCTAssertEqual(WhoopWire.crc8(Array("123456789".utf8)), 0xf4)
    }
    func testFragmentedAndCoalescedFramesRejectCorruption() throws {
        let packet = WhoopWire.command(0x16, sequence: 3, payload: [0])
        var reassembler = Reassembler()
        XCTAssertEqual(reassembler.append(Data([0, 1, 2]) + packet.prefix(5)).count, 0)
        let frames = reassembler.append(packet.dropFirst(5) + packet)
        XCTAssertEqual(frames, [packet, packet]); XCTAssertEqual(try Frame(frames[0]).command, 0x16)
        var corrupt = packet; corrupt[7] ^= 1
        XCTAssertThrowsError(try Frame(corrupt))
    }
    func testDurableCaptureAndUploadAcknowledgement() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: url.path + suffix) } }
        var capture = Capture(deviceId: "strap", sessionId: "session", source: "history", raw: Data([1]))
        capture.useHistoricalIdentity()
        do {
            let store = try CaptureStore(url: url)
            try store.insert(capture); try store.insert(capture)
            XCTAssertEqual(try store.pending().count, 1)
        }
        let reopened = try CaptureStore(url: url)
        XCTAssertEqual(try reopened.pending().first?.id, capture.id)
        try reopened.acknowledge([capture.id])
        XCTAssertTrue(try reopened.pending().isEmpty)
    }
    func testStandaloneSnapshotIncludesUncheckpointedWAL() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try CaptureStore(url: folder.appendingPathComponent("live.sqlite"))
        let row = Capture(deviceId: "test", sessionId: "test", source: "live_hr", raw: Data([0, 70]))
        try store.insert(row)
        let snapshot = folder.appendingPathComponent("snapshot.sqlite")
        try store.snapshot(to: snapshot)
        XCTAssertEqual(try CaptureStore(url: snapshot).pending().map(\.id), [row.id])
        try store.acknowledge([row.id])
        XCTAssertEqual(try CaptureStore(url: snapshot).pending().count, 1)
    }
    func testNotificationBatchCommitsTogetherAndRollsBackOnFailure() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appendingPathComponent("captures.sqlite")
        let store = try CaptureStore(url: url)
        let reader = try CaptureStore(url: url, readOnly: true)
        let raw = Capture(deviceId: "test", sessionId: "test", source: "raw", raw: Data([1]))
        var invalid = Capture(deviceId: "test", sessionId: "test", source: "history", raw: Data([2]))
        invalid.sampleAt = .nan // JSON encoding fails after the first INSERT.
        XCTAssertThrowsError(try store.insert([raw, invalid]))
        XCTAssertTrue(try reader.pending().isEmpty, "Never expose a partial notification")
        XCTAssertEqual(try reader.exportStatus().pending, 0, "Rollback also restores the pending-count trigger")
        invalid.sampleAt = 100
        try store.insert([raw, invalid])
        XCTAssertEqual(try reader.pending().map(\.id), [raw.id, invalid.id])
        XCTAssertEqual(try reader.exportStatus().pending, 2)
        try store.insert([raw, invalid])
        XCTAssertEqual(try reader.exportStatus().pending, 2, "Replays remain idempotent")
        XCTAssertThrowsError(try reader.insert(Capture(deviceId: "test", sessionId: "test", source: "raw", raw: Data())))
        XCTAssertEqual(try reader.exportStatus().pending, 2, "Chart connections cannot write")
    }
    func testBatteryCrossingsAndRechargeHysteresis() throws {
        var alerts = BatteryAlerts()
        XCTAssertNil(alerts.observe(70))
        XCTAssertEqual(alerts.observe(19), 20)
        XCTAssertNil(alerts.observe(18))
        XCTAssertNil(alerts.observe(21))
        XCTAssertEqual(alerts.observe(9), 10)
        let saved = try JSONEncoder().encode(alerts)
        alerts = try JSONDecoder().decode(BatteryAlerts.self, from: saved)
        XCTAssertNil(alerts.observe(8))
        XCTAssertNil(alerts.observe(80))
        XCTAssertEqual(alerts.observe(8), 10)
        XCTAssertNil(alerts.observe(8))
        XCTAssertNil(alerts.observe(255))
    }
}
