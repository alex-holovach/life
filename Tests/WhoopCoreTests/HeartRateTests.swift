import XCTest
@testable import WhoopCore

final class HeartRateTests: XCTestCase {
    private func capture(_ time: Double, bpm: Int = 72, quality: String = "live_receipt_timestamp") -> Capture {
        var c = Capture(deviceId: "strap", sessionId: "test", source: "live_hr", raw: Data(), receivedAt: time)
        c.hrBpm = bpm; c.quality = quality
        return c
    }
    func testChartRestoresAcknowledgedReadingsAndFiltersInvalidAndOutOfRange() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: url.path + suffix) } }
        let good = capture(100, bpm: 68), newer = capture(110, bpm: 80)
        do {
            let store = try CaptureStore(url: url)
            for c in [newer, capture(50), good, capture(120, bpm: 0), capture(130, quality: "off_wrist"), capture(300)] { try store.insert(c) }
            try store.acknowledge([good.id, newer.id])
        }
        let reopened = try CaptureStore(url: url)
        let rows = try reopened.heartRateReadings(since: Date(timeIntervalSince1970: 90), through: Date(timeIntervalSince1970: 200))
        XCTAssertEqual(rows.map(\.id), [good.id, newer.id])
        XCTAssertEqual(rows.map(\.bpm), [68, 80])
        XCTAssertEqual(try reopened.heartRateReadings(since: Date(timeIntervalSince1970: 90), through: Date(timeIntervalSince1970: 200), limit: 1).map(\.id), [newer.id])
    }
    func testDownsamplingPreservesExtremaAndNeverBridgesMissingReadings() {
        let captures = (0..<100).map { capture(Double($0), bpm: $0 == 21 ? 130 : $0 == 37 ? 42 : 70) }
            + (200..<250).map { capture(Double($0), bpm: 75) }
        let readings = captures.compactMap { HeartRateReading(capture: $0) }
        let points = HeartRateTimeline.points(readings, target: 20)
        XCTAssertLessThan(points.count, readings.count)
        XCTAssertTrue(points.contains { $0.reading.bpm == 130 })
        XCTAssertTrue(points.contains { $0.reading.bpm == 42 })
        let groups = Dictionary(grouping: points, by: \.segment)
        XCTAssertEqual(groups.count, 2)
        for group in groups.values {
            let times = group.map { $0.reading.at.timeIntervalSince1970 }
            XCTAssertTrue(times.allSatisfy { $0 < 100 } || times.allSatisfy { $0 >= 200 })
            XCTAssertEqual(times, times.sorted())
        }
        XCTAssertEqual(points.first?.reading.id, readings.first?.id)
        XCTAssertEqual(points.last?.reading.id, readings.last?.id)
    }
    func testHistoryFillsGapAtMeasurementTimeWithoutDuplicatingLiveReadings() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: url.path + suffix) } }
        let store = try CaptureStore(url: url)
        for time in [100.0, 101, 110] { try store.insert(capture(time)) }
        for time in [100.2, 105, 109.8] {
            var c = capture(500, bpm: 80, quality: "historical_intervals_unverified")
            c.source = "history"; c.sampleAt = time; try store.insert(c)
        }
        let rows = try store.heartRateReadings(since: Date(timeIntervalSince1970: 90), through: Date(timeIntervalSince1970: 120))
        XCTAssertEqual(rows.map { $0.at.timeIntervalSince1970 }, [100, 101, 105, 110])
        XCTAssertEqual(rows.map(\.bpm), [72, 72, 80, 72])
    }
    func testChartProjectionPreservesSourceQualityAndOriginalTimes() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: url.path + suffix) } }
        let store = try CaptureStore(url: url)
        var live = capture(100, bpm: 71)
        live.rawBase64 = Data(repeating: 42, count: 4096).base64EncodedString()
        live.intervalWords = [900, 901]; live.acceleration = [0.1, 0.2, 0.3]
        var historical = capture(500, bpm: 65, quality: "historical_intervals_unverified")
        historical.source = "history"; historical.sampleAt = 110
        var missingTime = historical; missingTime.id = "missing-time"; missingTime.sampleAt = nil
        var wrongSourceQuality = capture(115, quality: "historical_intervals_unverified")
        wrongSourceQuality.id = "wrong-quality"
        let offWrist = capture(120, quality: "off_wrist"), invalid = capture(125, bpm: 0)
        try store.insert([live, historical, missingTime, wrongSourceQuality, offWrist, invalid])
        try store.acknowledge([live.id, historical.id])
        let reader = try CaptureStore(url: url, readOnly: true)
        let readings = try reader.heartRateReadings(since: Date(timeIntervalSince1970: 90), through: Date(timeIntervalSince1970: 600))
        XCTAssertEqual(readings.map(\.id), [live.id, historical.id])
        XCTAssertEqual(readings.map(\.bpm), [71, 65])
        XCTAssertEqual(readings.map { $0.at.timeIntervalSince1970 }, [100, 110])
        XCTAssertEqual(readings.map(\.isHistorical), [false, true])
        XCTAssertEqual(try reader.heartRateReadings(since: Date(timeIntervalSince1970: 90), through: Date(timeIntervalSince1970: 600), limit: 0), [])
        XCTAssertEqual(try store.pending().map(\.id), [missingTime.id, wrongSourceQuality.id, offWrist.id, invalid.id])
    }
}
