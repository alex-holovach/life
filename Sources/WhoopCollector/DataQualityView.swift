import Foundation
import SwiftUI

struct DataQualityReport: Decodable {
    struct History: Decodable {
        let through: Double?
        let record_coverage: Double
        let records: Int
        let empty_interval_records: Int
    }
    struct Windows: Decodable {
        struct Point: Decodable { let at: Double; let value: Double }
        let analyzed_windows: Int
        let qualified_windows: Int
        let qualified_seconds: Double
        let reasons: [String: Int]
        let points: [Point]
    }
    let state: String
    let computed_at: Double?
    let history: History?
    let hrv: Windows?
    let respiration: Windows?

    func isCurrent(at now: Date) -> Bool {
        guard state == "ready", let computed_at, let history,
              hrv != nil, respiration != nil, history.record_coverage.isFinite,
              (0...1.001).contains(history.record_coverage) else { return false }
        return (0..<180).contains(now.timeIntervalSince1970-computed_at)
    }

    static func reason(_ code: String) -> String {
        switch code {
        case "too_few_adjacent_pairs": return "Unverified continuity between pulse batches"
        case "missing_history_records": return "Missing stored records"
        case "incomplete_pulse_coverage": return "Missing pulse duration"
        case "pulse_artifacts": return "Pulse artifacts"
        case "changing_heart_rate": return "Changing heart rate"
        case "short_contiguous_segment", "no_contiguous_intervals": return "Pulse segments too short"
        case "no_pulse_intervals": return "No pulse intervals from WHOOP"
        case "conflicting_record": return "Conflicting stored record"
        case "firmware_not_verified_for_window": return "Firmware not verified for this period"
        case "no_history": return "No stored records"
        default: return "Breathing rhythm not resolved"
        }
    }
}

struct DataQualityCard: View {
    let report: DataQualityReport?
    let message: String?
    let now: Date
    @State private var expanded = false

    var body: some View {
        if message == nil, let report, report.isCurrent(at: now),
           let history = report.history, let hrv = report.hrv, let breathing = report.respiration {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Label("DATA QUALITY", systemImage: "checkmark.shield")
                        .font(.system(size: 11, weight: .semibold)).tracking(1)
                    Spacer()
                    Text("PAST 24 HOURS").font(.system(size: 9, weight: .medium))
                }.foregroundStyle(LifeTheme.muted)
                if let through = history.through {
                    let delay = max(0, now.timeIntervalSince1970-through)
                    Text(delay > 600 ? "History catching up · \(Int(delay / 60)) min behind" : "Stored history is up to date")
                        .font(.subheadline).foregroundStyle(delay > 600 ? LifeTheme.coral : LifeTheme.mint)
                } else {
                    Text("Waiting for stored records").font(.subheadline)
                }
                qualityBar("Stored records", ratio: history.record_coverage, color: LifeTheme.mint)
                qualityBar("HRV window coverage", ratio: hrv.qualified_seconds / 86400, color: LifeTheme.mint)
                qualityBar("Breathing window coverage", ratio: breathing.qualified_seconds / 86400, color: LifeTheme.muted)
                DisclosureGroup("What limits the readings", isExpanded: $expanded) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("\(hrv.qualified_windows) of \(hrv.analyzed_windows) HRV windows passed. \(breathing.qualified_windows) of \(breathing.analyzed_windows) breathing windows passed.")
                        ForEach(hrv.reasons.sorted { $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value }.prefix(3), id: \.key) { reason in
                            Text("HRV: \(DataQualityReport.reason(reason.key)) · \(reason.value) windows")
                        }
                        Text("\(history.empty_interval_records) stored records contain no pulse intervals. This can occur at slow heart rates or when WHOOP withholds pulse output.")
                        Text("Coverage measures availability, not accuracy. Overlapping windows count their covered time once; failures can have several reasons.")
                    }.font(.caption).foregroundStyle(LifeTheme.muted).padding(.top, 10)
                }.font(.caption).tint(LifeTheme.mint)
            }.padding(22).frame(maxWidth: .infinity, alignment: .leading)
                .background(LifeTheme.card, in: RoundedRectangle(cornerRadius: 28))
                .overlay(RoundedRectangle(cornerRadius: 28).stroke(.white.opacity(0.055), lineWidth: 1))
        }
    }

    private func qualityBar(_ title: String, ratio: Double, color: Color) -> some View {
        VStack(spacing: 5) {
            HStack {
                Text(title)
                Spacer()
                Text("\(Int(min(1, max(0, ratio)) * 100))%").monospacedDigit()
            }.font(.caption).foregroundStyle(LifeTheme.muted)
            ProgressView(value: min(1, max(0, ratio))).tint(color)
        }
    }
}
