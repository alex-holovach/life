import Foundation
import Security
import Combine
import WhoopCore

enum Credentials {
    private static let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "whoop-local", kSecAttrAccount as String: "ingestion"]
    static func read() -> String {
        var q = query; q[kSecReturnData as String] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return "" }
        return String(decoding: data, as: UTF8.self)
    }
    static func save(_ value: String) throws {
        guard value.count >= 32, !value.contains(where: { $0.isWhitespace }) else { throw WireError.invalid }
        var q = query; q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(q as CFDictionary, nil)
        if status == errSecDuplicateItem {
            let update = [kSecValueData as String: Data(value.utf8)]
            guard SecItemUpdate(query as CFDictionary, update as CFDictionary) == errSecSuccess else { throw WireError.invalid }
        } else if status != errSecSuccess { throw WireError.invalid }
    }
}

final class Uploader: NSObject, ObservableObject, URLSessionTaskDelegate, URLSessionDelegate {
    private enum TransferState { case idle, message(String) }
    private var transferState = TransferState.message("Configure your HTTPS ingestion endpoint")
    @Published private(set) var status = "Configure your HTTPS ingestion endpoint"
    @Published var pendingCount = 0
    @Published var lastAcknowledged: Date?
    @Published var retryAt: Date?
    var backgroundCompletion: (() -> Void)?
    private let store: CaptureStore
    private let directory: URL
    private let defaults: UserDefaults
    private let readToken: () -> String
    private let configuration: URLSessionConfiguration?
    private var checking = false
    private var lastAttempt = Date.distantPast
    private lazy var session: URLSession = {
        let config = configuration ?? URLSessionConfiguration.background(withIdentifier: "whoop-local.upload.v1")
        config.sessionSendsLaunchEvents = true; config.isDiscretionary = false
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()
    init(store: CaptureStore, directory: URL, defaults: UserDefaults = .standard,
         readToken: @escaping () -> String = Credentials.read, configuration: URLSessionConfiguration? = nil) {
        self.store = store; self.directory = directory; self.defaults = defaults
        self.readToken = readToken; self.configuration = configuration; super.init()
    }
    func restore() {
        _ = session; refreshStatus()
        let epoch = defaults.double(forKey: "uploadRetryAt")
        retryAt = epoch > 0 ? Date(timeIntervalSince1970: epoch) : nil
    }
    func refreshStatus() {
        do {
            let snapshot = try store.exportStatus()
            pendingCount = snapshot.pending; lastAcknowledged = snapshot.lastAcknowledged
            renderStatus()
        } catch { show(.message("Cannot read export queue: \(error.localizedDescription)")) }
    }
    private func show(_ state: TransferState) {
        transferState = state; renderStatus()
    }
    private func renderStatus() {
        switch transferState {
        case .idle:
            status = pendingCount > 0 ? "Waiting to upload to backend" :
                (lastAcknowledged == nil ? "No captured data yet" : "All captured data saved on backend")
        case .message(let message): status = message
        }
    }
    func sync(force: Bool = false) {
        refreshStatus()
        guard !checking, force || Date().timeIntervalSince(lastAttempt) > 60 else { return }
        if !force, let retryAt, retryAt > Date() { return }
        guard let text = defaults.string(forKey: "ingestionURL"),
              let url = URL(string: text), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.path == "/v1/batches",
              url.query == nil, url.fragment == nil else { show(.message("Set the HTTPS /v1/batches endpoint")); return }
        let token = readToken(); guard !token.isEmpty else { show(.message("Save your ingestion token")); return }
        checking = true; lastAttempt = Date()
        session.getAllTasks { tasks in
            DispatchQueue.main.async {
                defer { self.checking = false }
                guard tasks.isEmpty else { self.show(.message("Uploading to backend")); return }
                // Orphan files have no active task; their rows remain in SQLite for replay.
                if let files = try? FileManager.default.contentsOfDirectory(at: self.directory, includingPropertiesForKeys: nil) {
                    for file in files where file.lastPathComponent.hasPrefix("upload-") && file.pathExtension == "json" {
                        try? FileManager.default.removeItem(at: file)
                    }
                }
                do {
                    var captures: [Capture] = []; var size = 0
                    for capture in try self.store.pending() {
                        let nextSize = try JSONEncoder().encode(capture).count + 1
                        if size + nextSize > 1_500_000 { break }
                        captures.append(capture); size += nextSize
                    }
                    guard !captures.isEmpty else { self.show(.idle); return }
                    let file = self.directory.appendingPathComponent("upload-\(UUID().uuidString).json")
                    try JSONEncoder().encode(["captures": captures]).write(to: file, options: .atomic)
                    var request = URLRequest(url: url)
                    request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                    let task = self.session.uploadTask(with: request, fromFile: file)
                    task.taskDescription = file.lastPathComponent; task.resume(); self.show(.message("Upload queued"))
                } catch { self.show(.message("Upload preparation failed: \(error.localizedDescription)")) }
            }
        }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let name = task.taskDescription, name.hasPrefix("upload-"), !name.contains("/") else { return }
        let file = directory.appendingPathComponent(name)
        let code = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        if error == nil, code == 204 {
            do {
                let batch = try JSONDecoder().decode([String: [Capture]].self, from: Data(contentsOf: file))
                guard let captures = batch["captures"] else { throw WireError.invalid }
                try store.acknowledge(captures.map(\.id))
                transferState = .idle
                retryAt = nil; defaults.removeObject(forKey: "uploadRetryAt")
                defaults.set(0, forKey: "uploadFailureCount")
                refreshStatus()
                DispatchQueue.main.async { self.sync(force: true) }
            } catch { scheduleRetry("Local acknowledgement failed; records retained") }
        } else {
            let message = error != nil ? "Backend unreachable; records retained" :
                ([401, 403].contains(code) ? "Server rejected token" : "Upload failed (HTTP \(code)); records retained")
            scheduleRetry(message)
        }
        try? FileManager.default.removeItem(at: file)
        refreshStatus()
    }
    private func scheduleRetry(_ message: String) {
        let attempts = min(defaults.integer(forKey: "uploadFailureCount") + 1, 7)
        let next = Date().addingTimeInterval(min(3600, 30 * pow(2, Double(attempts))))
        defaults.set(attempts, forKey: "uploadFailureCount")
        defaults.set(next.timeIntervalSince1970, forKey: "uploadRetryAt")
        retryAt = next; show(.message(message))
        // iOS may suspend this timer; BLE and foreground wakeups also honor the deadline.
        DispatchQueue.main.asyncAfter(deadline: .now() + next.timeIntervalSinceNow) { [weak self] in self?.sync() }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        backgroundCompletion?(); backgroundCompletion = nil
    }
}
