import Foundation
import SwiftUI
import Charts

struct TemperatureSnapshot: Decodable {
    struct Point: Decodable { let at: Double; let celsius: Double; let segment: Int }
    let state: String
    let celsius: Double?
    let observed_at: Double?
    let decoder: String?
    let points: [Point]

    func isCurrent(at now: Date) -> Bool {
        guard state == "ready", let at = observed_at, let value = celsius,
              decoder != nil, value.isFinite, (0..<70).contains(value) else { return false }
        return (0...600).contains(now.timeIntervalSince1970 - at)
    }
}

private final class TemperatureRedirectPolicy: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

@MainActor
final class TemperatureClient: ObservableObject {
    @Published private(set) var snapshot: TemperatureSnapshot?
    @Published private(set) var message: String?
    private let redirectPolicy = TemperatureRedirectPolicy()
    private lazy var session = URLSession(configuration: .ephemeral, delegate: redirectPolicy, delegateQueue: nil)

    func refresh() async {
        guard let text = UserDefaults.standard.string(forKey: "ingestionURL"),
              var parts = URLComponents(string: text), parts.scheme == "https", parts.host != nil,
              parts.user == nil, parts.password == nil, parts.path == "/v1/batches",
              parts.query == nil, parts.fragment == nil else { message = "Connect your backend"; return }
        let token = Credentials.read()
        guard !token.isEmpty else { message = "Connect your backend"; return }
        parts.path = "/v1/temperature"
        guard let url = parts.url else { return }
        var request = URLRequest(url: url); request.timeoutInterval = 12
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        do {
            let (data, response) = try await session.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
            snapshot = try JSONDecoder().decode(TemperatureSnapshot.self, from: data); message = nil
        } catch {
            if !Task.isCancelled { message = "Backend unavailable" }
        }
    }
}

struct TemperatureCard: View {
    let snapshot: TemperatureSnapshot?
    let message: String?
    let now: Date
    private let amber = Color(red: 0.97, green: 0.77, blue: 0.48)
    private var current: Bool { message == nil && snapshot?.isCurrent(at: now) == true }
    private var status: String {
        if let message { return message }
        switch snapshot?.state {
        case "ready" where current: return "From WHOOP’s skin sensor"
        case "stale", "ready": return "Waiting for a fresh reading"
        case "sensor_unavailable": return "Sensor reading unavailable"
        case "unsupported_firmware": return "Temperature decoder needs an update"
        case "awaiting_firmware": return "Waiting for WHOOP’s firmware details"
        default: return "Waiting for stored sensor readings"
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("SKIN TEMPERATURE", systemImage: "thermometer.medium")
                    .font(.system(size: 11, weight: .semibold)).tracking(1.5).foregroundStyle(amber)
                Spacer()
                Text("WRIST").font(.system(size: 9, weight: .medium)).tracking(1.5).foregroundStyle(LifeTheme.muted)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(current ? String(format: "%.1f", snapshot!.celsius!) : "—")
                    .font(.system(size: 38, weight: .regular, design: .rounded)).monospacedDigit()
                Text("°C").font(.title3).foregroundStyle(LifeTheme.muted)
            }
            Text(status).font(.caption).foregroundStyle(LifeTheme.muted)
            if let points = snapshot?.points, points.count > 1 {
                Chart(Array(points.enumerated()), id: \.offset) { _, point in
                    LineMark(x: .value("Time", Date(timeIntervalSince1970: point.at)), y: .value("Skin temperature", point.celsius), series: .value("Continuous readings", point.segment))
                        .foregroundStyle(amber).lineStyle(StrokeStyle(lineWidth: 2))
                }.chartYScale(domain: .automatic(includesZero: false)).frame(height: 95)
            }
            if let at = snapshot?.observed_at {
                Text("Measured \(Date(timeIntervalSince1970: at).formatted(date: .omitted, time: .shortened))")
                    .font(.caption2).foregroundStyle(LifeTheme.muted)
            }
        }.frame(maxWidth: .infinity, alignment: .leading).padding(22)
            .background(LifeTheme.card, in: RoundedRectangle(cornerRadius: 28))
            .overlay(RoundedRectangle(cornerRadius: 28).stroke(.white.opacity(0.055), lineWidth: 1))
            .accessibilityElement(children: .combine)
    }
}
