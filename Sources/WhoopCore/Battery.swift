import Foundation

public struct BatteryEvidence: Codable {
    public struct Reading: Codable {
        public var percent: Double
        public var at: Date
    }
    public var standard: Reading?
    public var custom: Reading?
    public init() {}
    public mutating func observe(_ percent: Double, custom isCustom: Bool, at date: Date) {
        guard percent.isFinite, (0...100).contains(percent) else { return }
        let reading = Reading(percent: percent, at: date)
        if isCustom { custom = reading } else { standard = reading }
    }
    public func verified(at now: Date) -> Reading? {
        guard let standard, let custom,
              (0...600).contains(now.timeIntervalSince(standard.at)),
              (0...600).contains(now.timeIntervalSince(custom.at)),
              abs(standard.at.timeIntervalSince(custom.at)) <= 30,
              abs(standard.percent - custom.percent) <= 1 else { return nil }
        return Reading(percent: custom.percent, at: custom.at)
    }
    public func status(at now: Date) -> String {
        guard standard != nil || custom != nil else { return "Not read yet" }
        let newest = max(standard?.at ?? .distantPast, custom?.at ?? .distantPast)
        guard now.timeIntervalSince(newest) <= 600 else { return "Stale — waiting for a fresh reading" }
        if let reading = verified(at: now) { return String(format: "%.1f%% · cross-checked", reading.percent) }
        if let standard, let custom, abs(standard.at.timeIntervalSince(custom.at)) <= 30,
           abs(standard.percent - custom.percent) > 1 {
            return String(format: "Unverified · standard %.0f%% / custom %.1f%%", standard.percent, custom.percent)
        }
        return "Unverified — waiting for both battery readings"
    }
}

/// WHOOP 4 v24 burst/continuation verified on hardware. HistoryTransfer additionally
/// gates every ACK on durable storage, record continuity and the observed marker layout.
public enum HistorySafety {
    public static let requestsEnabled = true
    public static let acknowledgementsEnabled = true
}
