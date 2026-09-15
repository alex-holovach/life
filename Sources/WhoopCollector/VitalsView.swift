import Foundation
import SwiftUI

struct VitalsSnapshot: Decodable {
    struct Oxygen: Decodable {
        let state: String
        let percent: Double?
        let observed_at: Double?
        let decoder: String
        func isCurrent(at now: Date) -> Bool {
            guard state == "ready", decoder == "harvard_41_17_4_0_spo2_v1",
                  let percent, percent.isFinite, (70...100).contains(percent), percent != 98,
                  let observed_at, (0...86400).contains(now.timeIntervalSince1970-observed_at) else { return false }
            return true
        }
    }
    struct Respiration: Decodable {
        let state: String
        let breaths_per_minute: Double?
        let end: Double?
        let algorithm: String
        let quality: String
        let duration_seconds: Double
        let coverage: Double
        var reasons: [String]? = nil
        var history_end: Double? = nil
        func isCurrent(at now: Date) -> Bool {
            isAvailable(at: now) && state == "ready" && now.timeIntervalSince1970-end! <= 900
        }
        func isAvailable(at now: Date) -> Bool {
            guard ["ready", "stale"].contains(state), algorithm == "r24_pulse_respiration_v1", quality == "signal_checks_passed",
                  let breaths_per_minute, breaths_per_minute.isFinite, (6...30).contains(breaths_per_minute),
                  let end, (0..<86400).contains(now.timeIntervalSince1970-end), duration_seconds >= 90,
                  (0.98...1.02).contains(coverage) else { return false }
            return true
        }
        var signalDetail: String {
            let reasons = Set(reasons ?? [])
            if !reasons.isDisjoint(with: ["short_contiguous_segment", "no_contiguous_intervals"]) {
                return "No verified 90-second pulse segment. Empty batches can occur at slow heart rates."
            }
            if reasons.contains("incomplete_pulse_coverage") { return "Too much pulse timing is missing" }
            if !reasons.isDisjoint(with: ["pulse_artifacts", "changing_heart_rate"]) { return "Pulse artifacts or changing heart rate" }
            if !reasons.isEmpty { return "No consistent breathing rhythm in pulse timing" }
            return "No qualifying pulse recording yet"
        }
    }
    let oxygen: Oxygen
    let respiration: Respiration
}

private final class VitalsRedirectPolicy: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

@MainActor
final class VitalsClient: ObservableObject {
    @Published private(set) var snapshot: VitalsSnapshot?
    @Published private(set) var message: String?
    private let policy = VitalsRedirectPolicy()
    private lazy var session = URLSession(configuration: .ephemeral, delegate: policy, delegateQueue: nil)

    func refresh() async {
        guard let text = UserDefaults.standard.string(forKey: "ingestionURL"),
              var parts = URLComponents(string: text), parts.scheme == "https", parts.host != nil,
              parts.user == nil, parts.password == nil, parts.path == "/v1/batches",
              parts.query == nil, parts.fragment == nil, !Credentials.read().isEmpty else {
            snapshot = nil; message = "Connect your backend"; return
        }
        parts.path = "/v1/vitals"
        guard let url = parts.url else { return }
        var request = URLRequest(url: url); request.timeoutInterval = 12
        request.setValue("Bearer \(Credentials.read())", forHTTPHeaderField: "Authorization")
        do {
            let (data, response) = try await session.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
            snapshot = try JSONDecoder().decode(VitalsSnapshot.self, from: data); message = nil
        } catch { if !Task.isCancelled { message = "Backend unavailable" } }
    }
}

struct VitalsCards: View {
    let snapshot: VitalsSnapshot?
    let message: String?
    let now: Date
    private var oxygenReady: Bool { message == nil && snapshot?.oxygen.isCurrent(at: now) == true }
    private var respirationReady: Bool { message == nil && snapshot?.respiration.isAvailable(at: now) == true }

    private func status(_ state: String?, oxygen: Bool) -> String {
        if let message { return message }
        if !oxygen, let through = snapshot?.respiration.history_end, now.timeIntervalSince1970-through > 600,
           !["unsupported_firmware", "analysis_unavailable"].contains(state ?? "") {
            return "Stored readings are syncing"
        }
        switch state {
        case "unsupported_firmware": return "Waiting for verified firmware"
        case "analysis_unavailable": return "Waiting for backend analysis"
        case "ambiguous_firmware_value": return "Device result is ambiguous"
        case "device_signal_unavailable", "conflicting_record": return "Device signal unavailable"
        case "stale", "ready": return "Waiting for a fresh reading"
        default: return oxygen ? "Waiting for WHOOP’s oxygen reading" : snapshot?.respiration.signalDetail ?? "No qualifying pulse recording yet"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                card(title: "Breathing", symbol: "lungs", color: LifeTheme.mint,
                     value: respirationReady ? String(format: "%.0f", snapshot!.respiration.breaths_per_minute!) : "—",
                     unit: "breaths/min · estimated",
                     detail: respirationReady ? "Last qualifying estimate" : status(snapshot?.respiration.state, oxygen: false),
                     at: respirationReady ? snapshot?.respiration.end : nil)
                card(title: "Blood oxygen", symbol: "drop", color: Color(red: 0.75, green: 0.72, blue: 0.96),
                     value: oxygenReady ? String(format: "%.0f", snapshot!.oxygen.percent!) : "—",
                     unit: "% SpO₂",
                     detail: oxygenReady ? "Device calculation" : status(snapshot?.oxygen.state, oxygen: true),
                     at: oxygenReady ? snapshot?.oxygen.observed_at : nil)
            }
            Text("Breathing uses pulse timing. Oxygen uses WHOOP’s calculation. Accuracy has not been checked against a reference.")
                .font(.caption2).foregroundStyle(LifeTheme.muted).padding(.horizontal, 6)
        }
    }

    private func card(title: String, symbol: String, color: Color, value: String,
                      unit: String, detail: String, at: Double?) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: symbol).font(.system(size: 12, weight: .medium)).foregroundStyle(color)
            Text(value).font(.system(size: 38, weight: .regular, design: .rounded)).monospacedDigit()
            Text(unit).font(.caption2).foregroundStyle(LifeTheme.muted)
            Text(detail).font(.caption).foregroundStyle(LifeTheme.muted).fixedSize(horizontal: false, vertical: true)
            if let at {
                Text("Recorded \(Date(timeIntervalSince1970: at).formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption2).foregroundStyle(LifeTheme.muted)
            }
        }.frame(maxWidth: .infinity, minHeight: 185, alignment: .topLeading).padding(18)
            .background(LifeTheme.card, in: RoundedRectangle(cornerRadius: 26))
            .overlay(RoundedRectangle(cornerRadius: 26).stroke(.white.opacity(0.055), lineWidth: 1))
            .accessibilityElement(children: .combine)
    }
}
