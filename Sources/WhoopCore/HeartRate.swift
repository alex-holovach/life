import Foundation

public struct HeartRateReading: Identifiable, Equatable, Sendable {
    public let id: String
    public let at: Date
    public let bpm: Int
    public let isHistorical: Bool

    public init?(capture: Capture) {
        self.init(id: capture.id, source: capture.source, quality: capture.quality,
                  receivedAt: capture.receivedAt, sampleAt: capture.sampleAt, bpm: capture.hrBpm)
    }
    // The store projects these fields without decoding raw packets and sensor
    // arrays. Both paths use the same eligibility and timestamp rules.
    init?(id: String, source: String, quality: String?, receivedAt: Double, sampleAt: Double?, bpm: Int?) {
        let historical = source == "history" && quality == "historical_intervals_unverified"
        let live = source == "live_hr" && quality == "live_receipt_timestamp"
        let timestamp = historical ? sampleAt : receivedAt
        guard live || historical, let timestamp, timestamp.isFinite,
              let bpm, (1...255).contains(bpm) else { return nil }
        self.id = id
        self.at = Date(timeIntervalSince1970: timestamp)
        self.bpm = bpm
        self.isHistorical = historical
    }
}

public struct HeartRatePoint: Identifiable, Sendable {
    public let reading: HeartRateReading
    public let segment: String
    public var id: String { reading.id }
}

public enum HeartRateTimeline {
    /// Prefer directly received readings where they overlap, keeping historical
    /// samples at their measurement time to fill Bluetooth gaps.
    public static func mergingHistory(_ readings: [HeartRateReading]) -> [HeartRateReading] {
        let sorted = readings.sorted { $0.at < $1.at }
        let live = sorted.filter { !$0.isHistorical }
        var i = 0
        return sorted.filter { row in
            guard row.isHistorical else { return true }
            while i < live.count, live[i].at < row.at.addingTimeInterval(-1.5) { i += 1 }
            return i == live.count || abs(live[i].at.timeIntervalSince(row.at)) > 1.5
        }
    }
    /// Split gaps before reducing points, retaining each bucket's extrema and endpoints.
    /// The full readings remain available for statistics and chart selection.
    public static func points(_ readings: [HeartRateReading], target: Int = 360, gap: TimeInterval = 8) -> [HeartRatePoint] {
        guard !readings.isEmpty else { return [] }
        let width = max(1, Int(ceil(Double(readings.count) / Double(max(4, target) / 4))))
        var output: [HeartRatePoint] = []
        var start = 0
        while start < readings.count {
            var end = start + 1
            while end < readings.count, readings[end].at.timeIntervalSince(readings[end - 1].at) <= gap { end += 1 }
            let segment = readings[start].id
            for offset in stride(from: start, to: end, by: width) {
                let upper = min(offset + width, end)
                let indices = Array(offset..<upper)
                let low = indices.min { readings[$0].bpm < readings[$1].bpm }!
                let high = indices.max { readings[$0].bpm < readings[$1].bpm }!
                for index in Set([offset, upper - 1, low, high]).sorted() {
                    output.append(HeartRatePoint(reading: readings[index], segment: segment))
                }
            }
            start = end
        }
        return output
    }
}
