import Foundation
import SwiftUI
import Charts

struct HRVSnapshot: Decodable {
    struct Attempt: Decodable { let coverage: Double; let end: Double; let reasons: [String] }
    let state: String
    let rmssd_ms: Double?
    let end: Double?
    let algorithm: String
    let coverage: Double
    let pairs: Int
    let latest_attempt: Attempt?
    var history_end: Double? = nil
    var quality_report: DataQualityReport? = nil

    func isCurrent(at now: Date) -> Bool {
        isAvailable(at: now) && state == "ready" && now.timeIntervalSince1970-end! <= 900
    }

    func isAvailable(at now: Date) -> Bool {
        guard ["ready", "stale"].contains(state), algorithm == "r24_rmssd_5m_v1", let value = rmssd_ms,
              value.isFinite, value >= 0, let end, (0..<86400).contains(now.timeIntervalSince1970-end),
              (0.98...1.02).contains(coverage), pairs >= 180 else { return false }
        return true
    }
}

private final class HRVRedirectPolicy: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

@MainActor
final class HRVClient: ObservableObject {
    @Published private(set) var snapshot: HRVSnapshot?
    @Published private(set) var message: String?
    private let policy = HRVRedirectPolicy()
    private lazy var session = URLSession(configuration: .ephemeral, delegate: policy, delegateQueue: nil)

    func refresh() async {
        guard let text = UserDefaults.standard.string(forKey: "ingestionURL"),
              var parts = URLComponents(string: text), parts.scheme == "https", parts.host != nil,
              parts.user == nil, parts.password == nil, parts.path == "/v1/batches",
              parts.query == nil, parts.fragment == nil, !Credentials.read().isEmpty else {
            snapshot = nil; message = "Connect your backend"; return
        }
        parts.path = "/v1/hrv"
        guard let url = parts.url else { return }
        var request = URLRequest(url: url); request.timeoutInterval = 12
        request.setValue("Bearer \(Credentials.read())", forHTTPHeaderField: "Authorization")
        do {
            let (data, response) = try await session.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
            snapshot = try JSONDecoder().decode(HRVSnapshot.self, from: data); message = nil
        } catch { if !Task.isCancelled { message = "Backend unavailable" } }
    }
}

struct HRVCard: View {
    let snapshot: HRVSnapshot?
    let message: String?
    let now: Date
    var available: Bool { message == nil && snapshot?.isAvailable(at: now) == true }
    private var status: String {
        if let message { return message }
        switch snapshot?.state {
        case "ready" where available, "stale" where available: return "Last qualifying five-minute window"
        case "stale", "ready": return "No qualifying window in the past 24 hours"
        case "unsupported_firmware": return "Waiting for verified WHOOP firmware"
        case "analysis_unavailable": return "Waiting for backend analysis"
        case "insufficient_data": return "Insufficient clean pulse intervals"
        default: return "Waiting for five minutes of stored readings"
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label("HEART RATE VARIABILITY", systemImage: "waveform.path")
                    .font(.system(size: 11, weight: .semibold)).tracking(1).foregroundStyle(LifeTheme.mint)
                Spacer()
                Text("RMSSD").font(.system(size: 10, weight: .medium)).foregroundStyle(LifeTheme.muted)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(available ? String(format: "%.0f", snapshot!.rmssd_ms!) : "—")
                    .font(.system(size: 38, weight: .regular, design: .rounded)).monospacedDigit()
                Text("ms").font(.title3).foregroundStyle(LifeTheme.muted)
            }
            Text(status).font(.caption).foregroundStyle(LifeTheme.muted)
            if available, let snapshot, let end = snapshot.end {
                Text("\(snapshot.pairs) adjacent pairs · \(Int(min(1, snapshot.coverage)*100))% coverage")
                    .font(.caption).foregroundStyle(LifeTheme.muted)
                Text("Recorded \(Date(timeIntervalSince1970: end).formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption2).foregroundStyle(LifeTheme.muted)
            }
            if let attempt = snapshot?.latest_attempt, !available || attempt.end != snapshot?.end {
                Text("Latest window: \(Int(min(1, max(0, attempt.coverage))*100))% pulse coverage")
                    .font(.caption).foregroundStyle(LifeTheme.muted)
                if let reason = attempt.reasons.first {
                    Text(DataQualityReport.reason(reason)).font(.caption).foregroundStyle(LifeTheme.muted)
                }
            }
            if message == nil, let quality = snapshot?.quality_report, quality.isCurrent(at: now),
               let points = quality.hrv?.points, !points.isEmpty {
                Chart(Array(points.enumerated()), id: \.offset) { _, point in
                    PointMark(x: .value("Measured", Date(timeIntervalSince1970: point.at)),
                              y: .value("Five-minute RMSSD", point.value))
                        .foregroundStyle(LifeTheme.mint).symbolSize(10)
                }.chartYScale(domain: .automatic(includesZero: false))
                    .chartXScale(domain: now.addingTimeInterval(-86400)...now)
                    .chartXAxis {
                        AxisMarks(values: .stride(by: .hour, count: 6)) { _ in
                            AxisGridLine()
                            AxisValueLabel(format: .dateTime.hour())
                        }
                    }.frame(height: 95)
                Text("Qualifying five-minute readings · past 24 hours. Gaps are preserved.")
                    .font(.caption2).foregroundStyle(LifeTheme.muted)
            }
            if let through = snapshot?.history_end, now.timeIntervalSince1970-through > 600 {
                Text("Stored readings are syncing · received through \(Date(timeIntervalSince1970: through).formatted(date: .omitted, time: .shortened))")
                    .font(.caption).foregroundStyle(LifeTheme.muted)
            }
            Text("From optical pulse intervals. This is a five-minute reading, not a whole-night average.")
                .font(.caption2).foregroundStyle(LifeTheme.muted)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(22)
            .background(LifeTheme.card, in: RoundedRectangle(cornerRadius: 28))
            .overlay(RoundedRectangle(cornerRadius: 28).stroke(.white.opacity(0.055), lineWidth: 1))
            .accessibilityElement(children: .combine)
    }
}
