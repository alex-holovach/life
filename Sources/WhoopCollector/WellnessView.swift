import Foundation
import SwiftUI

struct WellnessProfile: Codable, Equatable {
    var timezone: String?
    var max_hr: Double?
    var sleep_goal_minutes: Double?
}
struct SleepSession: Codable, Identifiable, Equatable {
    var id: String
    var revision: Int
    var start: Double
    var end: Double?
    var awake_seconds: Double
    var updated_at: Double?
    var edit: Edit { Edit(revision: revision, start: start, end: end, awake_seconds: awake_seconds) }
    struct Edit: Encodable { let revision: Int; let start: Double; let end: Double?; let awake_seconds: Double }
}
struct WellnessSnapshot: Decodable {
    struct Load: Decodable {
        let state: String; let score: Double?; let coverage: Double
        let recorded_seconds: Double; let load: Double?; let end: Double
        let algorithm: String
    }
    struct Sleep: Decodable {
        let id: String; let score: Double?; let duration_seconds: Double?; let end: Double?
        let heart_rate_bpm: Double?; let hrv_ms: Double?; let temperature_celsius: Double?
        let temperature_delta_celsius: Double?; let baseline_nights: Int
        let source: String; let algorithm: String
        var hrv_coverage: Double? = nil
    }
    let state: String; let profile: WellnessProfile; let sessions: [SleepSession]
    let strain: Load?; let sleep: Sleep?; let computed_at: Double?
    func current(at now: Date) -> Bool {
        state == "ready" && computed_at.map { (0..<180).contains(now.timeIntervalSince1970 - $0) } == true
    }
}

/// Durable edits are separate from telemetry. Retries send the same revision and payload.
struct WellnessJournal: Codable {
    var backend = ""
    var profile: WellnessProfile?
    var profilePending = false
    var sessions: [SleepSession] = []
    var pending: Set<String> = []
    mutating func acknowledge(_ sent: SleepSession) {
        if sessions.first(where: { $0.id == sent.id }) == sent { pending.remove(sent.id) }
    }
    mutating func merge(_ remote: [SleepSession]) {
        for row in remote where !pending.contains(row.id) {
            sessions.removeAll { $0.id == row.id }; sessions.append(row)
        }
        sessions.sort { $0.start > $1.start }
    }
}
private final class WellnessRedirectPolicy: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
@MainActor final class WellnessClient: ObservableObject {
    @Published private(set) var journal = WellnessJournal()
    @Published private(set) var snapshot: WellnessSnapshot?
    @Published private(set) var message: String?
    private var busy = false
    private var loadFailed = false
    private let file: URL
    private let policy = WellnessRedirectPolicy()
    private lazy var session = URLSession(configuration: .ephemeral, delegate: policy, delegateQueue: nil)
    var active: SleepSession? { journal.sessions.first { $0.end == nil } }
    var hasPending: Bool { journal.profilePending || !journal.pending.isEmpty }
    var endpoint: String { UserDefaults.standard.string(forKey: "ingestionURL") ?? "" }
    init() {
        file = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Life/wellness.json")
        do {
            if FileManager.default.fileExists(atPath: file.path) { journal = try JSONDecoder().decode(WellnessJournal.self, from: Data(contentsOf: file)) }
        } catch { loadFailed = true; message = "Cannot read sleep journal. Your saved file has been preserved." }
    }
    private func store(_ value: WellnessJournal) throws {
        guard !loadFailed else { throw URLError(.cannotWriteToFile) }
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(value)
        #if os(iOS)
        try data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try data.write(to: file, options: .atomic)
        #endif
        journal = value
    }
    private func bind(_ value: inout WellnessJournal) throws {
        if value.backend.isEmpty { value.backend = endpoint }
        guard value.backend == endpoint else { throw JournalError.message("Reconnect the backend used by this sleep journal before editing or syncing.") }
    }
    enum JournalError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
    }
    func saveProfile(maxHR: Double?, goal: Double?) -> Bool {
        do {
            var value = journal; try bind(&value)
            value.profile = WellnessProfile(timezone: TimeZone.current.identifier, max_hr: maxHR, sleep_goal_minutes: goal)
            value.profilePending = true; try store(value); snapshot = nil; message = nil
            Task { await refresh() }; return true
        } catch { message = error.localizedDescription; return false }
    }
    func saveSleep(_ row: SleepSession) -> Bool {
        let now = Date.now.timeIntervalSince1970
        guard row.start <= now, row.awake_seconds >= 0,
              row.end.map({ $0 <= now && $0 > row.start && $0-row.start <= 86400 && row.awake_seconds < $0-row.start }) ?? (row.awake_seconds == 0) else {
            message = "Check the sleep times and awake minutes."; return false
        }
        guard !journal.sessions.contains(where: { $0.id != row.id && $0.start < (row.end ?? .infinity) && ($0.end ?? .infinity) > row.start }) else {
            message = "Sleep sessions cannot overlap."; return false
        }
        do {
            var value = journal; try bind(&value)
            value.sessions.removeAll { $0.id == row.id }; value.sessions.append(row)
            value.sessions.sort { $0.start > $1.start }; value.pending.insert(row.id)
            try store(value); snapshot = nil; message = nil; Task { await refresh() }; return true
        } catch { message = error.localizedDescription; return false }
    }
    func toggleSleep() {
        if var row = active { row.end = Date.now.timeIntervalSince1970; row.revision += 1; _ = saveSleep(row) }
        else { _ = saveSleep(SleepSession(id: UUID().uuidString.lowercased(), revision: 1, start: Date.now.timeIntervalSince1970, end: nil, awake_seconds: 0)) }
    }
    private func request(_ path: String, body: Data? = nil, backend: String) async throws -> Data {
        guard var parts = URLComponents(string: backend), parts.scheme == "https", parts.host != nil,
              parts.user == nil, parts.password == nil, parts.path == "/v1/batches", parts.query == nil,
              parts.fragment == nil, !Credentials.read().isEmpty else { throw JournalError.message("Connect your backend in Settings.") }
        parts.path = path
        guard let url = parts.url else { throw URLError(.badURL) }
        var request = URLRequest(url: url); request.timeoutInterval = 12
        request.setValue("Bearer \(Credentials.read())", forHTTPHeaderField: "Authorization")
        if let body { request.httpMethod = "PUT"; request.httpBody = body; request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.data(for: request)
        guard let code = (response as? HTTPURLResponse)?.statusCode, code == 200 else {
            if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let text = object["detail"] as? String {
                throw JournalError.message(text)
            }
            throw JournalError.message("Backend unavailable. Edits remain saved on this phone.")
        }
        guard endpoint == backend else { throw JournalError.message("Backend changed. Sync paused.") }
        return data
    }
    func refresh() async {
        guard !busy, !loadFailed else { return }; busy = true; defer { busy = false }
        do {
            var bound = journal; try bind(&bound); if bound.backend != journal.backend { try store(bound) }
            let backend = endpoint
            if journal.profilePending, let sent = journal.profile {
                _ = try await request("/v1/profile", body: JSONEncoder().encode(sent), backend: backend)
                if journal.profile == sent { var value = journal; value.profilePending = false; try store(value) }
            }
            let pending = journal.sessions.filter { journal.pending.contains($0.id) }.sorted { $0.start < $1.start }
            for sent in pending {
                _ = try await request("/v1/sleep/\(sent.id)", body: JSONEncoder().encode(sent.edit), backend: backend)
                var value = journal; value.acknowledge(sent); try store(value)
            }
            let data = try await request("/v1/wellness", backend: backend)
            let remote = try JSONDecoder().decode(WellnessSnapshot.self, from: data)
            var value = journal; value.merge(remote.sessions)
            if !value.profilePending { value.profile = remote.profile }
            try store(value); snapshot = remote; message = nil
        } catch { snapshot = nil; if !Task.isCancelled { message = error.localizedDescription } }
    }
    func load(at now: Date) -> WellnessSnapshot.Load? {
        guard !hasPending, message == nil, snapshot?.current(at: now) == true,
              let load = snapshot?.strain, load.algorithm == "life_edwards_hrmax_v1", now.timeIntervalSince1970 < load.end else { return nil }
        return load
    }
    func sleep(at now: Date) -> WellnessSnapshot.Sleep? {
        guard !hasPending, message == nil, snapshot?.current(at: now) == true,
              let sleep = snapshot?.sleep, let end = sleep.end, (0..<129600).contains(now.timeIntervalSince1970-end),
              sleep.source == "self_reported", sleep.algorithm == "life_reported_duration_v1" else { return nil }
        return sleep
    }
}

struct WellnessCard: View {
    @ObservedObject var client: WellnessClient
    let now: Date
    @State private var settings = false
    @State private var edit: SleepSession?
    @State private var history = false
    @State private var addingSleep = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("STRAIN & SLEEP", systemImage: "moon.stars").font(.system(size: 11, weight: .semibold)).tracking(1)
                Spacer(); Button("Set up") { settings = true }.font(.caption)
            }.foregroundStyle(LifeTheme.mint)
            if let load = client.load(at: now), load.state != "set_timezone" {
                Text("\(Int(load.coverage*100))% of today recorded · \(Int(load.recorded_seconds/60)) min").font(.caption)
                Text(load.score == nil ? ((load.state == "set_max_hr" || load.state == "set_timezone") ? "Set your maximum heart rate and time zone to calculate strain." : "Waiting for ten minutes of recorded heart rate.") : "Cardio load \(String(format: "%.0f", load.load ?? 0)) · Life strain / 21\(load.state == "partial" ? " · partial day" : "")")
                    .font(.caption).foregroundStyle(LifeTheme.muted)
            } else { Text("Strain uses recorded heart rate and your configured maximum.").font(.caption).foregroundStyle(LifeTheme.muted) }
            Divider().overlay(LifeTheme.muted.opacity(0.3))
            if let active = client.active {
                Text("Bedtime \(Date(timeIntervalSince1970: active.start).formatted(date: .abbreviated, time: .shortened))").font(.caption)
            } else if let sleep = client.sleep(at: now), let duration = sleep.duration_seconds {
                Text("Last sleep: \(Int(duration/3600))h \(Int(duration.truncatingRemainder(dividingBy: 3600)/60))m · self-reported").font(.subheadline)
                if client.snapshot?.profile.sleep_goal_minutes == nil {
                    Button("Set sleep target to calculate your score") { settings = true }.font(.caption)
                } else {
                    Text("Score measures duration against your sleep target.").font(.caption).foregroundStyle(LifeTheme.muted)
                }
                if let hr = sleep.heart_rate_bpm { Text("Sleeping heart rate \(Int(hr.rounded())) bpm").font(.caption) }
                if let hrv = sleep.hrv_ms { Text("Sleeping HRV \(Int(hrv.rounded())) ms").font(.caption) }
                else if let coverage = sleep.hrv_coverage {
                    Text("Overnight HRV: \(Int(coverage*100))% qualified coverage; needs at least 50%.")
                        .font(.caption).foregroundStyle(LifeTheme.muted)
                }
                if let temp = sleep.temperature_celsius {
                    Text("Wrist temperature \(String(format: "%.1f", temp)) °C" + (sleep.temperature_delta_celsius.map { " · \(String(format: "%+.1f", $0)) °C vs baseline" } ?? " · \(sleep.baseline_nights)/7 baseline nights"))
                        .font(.caption)
                }
            } else { Text("Log bedtime and waking. Edit the times if you forget.").font(.caption).foregroundStyle(LifeTheme.muted) }
            HStack {
                Button(client.active == nil ? "Going to sleep" : "I'm awake", action: client.toggleSleep)
                    .buttonStyle(.borderedProminent).tint(LifeTheme.mint).foregroundStyle(LifeTheme.canvas)
                Spacer()
                if let row = client.active ?? client.journal.sessions.first { Button("Edit times") { edit = row }.font(.caption) }
            }
            HStack {
                Button { addingSleep = true } label: { Label("Log past sleep", systemImage: "plus") }
                Spacer()
                Button { history = true } label: { Label("Sleep history", systemImage: "clock.arrow.circlepath") }
            }.font(.caption).buttonStyle(.borderless)
            if client.hasPending { Text("Saved on this phone · waiting to sync").font(.caption2).foregroundStyle(LifeTheme.muted) }
            if let message = client.message { Text(message).font(.caption2).foregroundStyle(LifeTheme.coral) }
        }.padding(22).background(LifeTheme.card, in: RoundedRectangle(cornerRadius: 28))
            .sheet(isPresented: $settings) { WellnessSettings(client: client) }
            .sheet(item: $edit) { SleepEditor(client: client, row: $0) }
            .sheet(isPresented: $addingSleep) { SleepEditor(client: client) }
            .sheet(isPresented: $history) { SleepHistoryView(client: client) }
    }
}
struct WellnessSettings: View {
    @ObservedObject var client: WellnessClient
    @Environment(\.dismiss) private var dismiss
    @State private var maxHR = ""
    @State private var goal = ""
    @State private var error: String?
    var body: some View {
        NavigationStack {
            Form {
                Section("Strain") {
                    TextField("Maximum heart rate · bpm", text: $maxHR)
                    Text("Use your known maximum heart rate. Leave empty if unknown; Life won't invent one.").font(.caption)
                }
                Section("Sleep") {
                    TextField("Sleep target · hours", text: $goal)
                    Text("Your chosen sleep target, not a medical recommendation. Sleep score is logged duration / target.").font(.caption)
                }
                Section("Day boundaries") { Text(TimeZone.current.identifier); Text("Saving uses this time zone for daily strain.").font(.caption) }
                if let error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("Scoring settings")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) { Button("Save") {
                        let hr = Double(maxHR); let hours = Double(goal)
                        guard (maxHR.isEmpty || hr.map { (80...250).contains($0) } == true),
                              (goal.isEmpty || hours.map { (3...15).contains($0) } == true) else { error = "Enter a maximum of 80–250 bpm and a sleep target of 3–15 hours, or leave blank."; return }
                        if client.saveProfile(maxHR: hr, goal: hours.map { $0*60 }) { dismiss() } else { error = client.message }
                    } }
                }
        }.tint(LifeTheme.mint).preferredColorScheme(.dark)
            .onAppear { maxHR = client.journal.profile?.max_hr.map { String(format: "%g", $0) } ?? ""; goal = client.journal.profile?.sleep_goal_minutes.map { String(format: "%g", $0/60) } ?? "" }
    }
}
