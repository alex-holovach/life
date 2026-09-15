import SwiftUI
import WhoopCore
#if os(iOS)
import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // Bluetooth restoration can launch us without displaying a SwiftUI view.
        // Recreate the central manager during launch so iOS can deliver restoration.
        _ = Services.shared
        return true
    }
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        Services.shared.uploader.backgroundCompletion = completionHandler
        Services.shared.uploader.restore()
    }
}
#endif

final class Services {
    static let shared = Services()
    let collector: Collector
    let uploader: Uploader
    init() {
        UserDefaults.standard.register(defaults: ["ingestionURL": AppConfiguration.defaultEndpoint])
        collector = Collector(); uploader = Uploader(store: collector.store, directory: collector.directory)
        collector.onSaved = { [weak uploader] in uploader?.sync() }
        uploader.restore()
    }
}

@main
struct LifeApp: App {
    #if os(iOS)
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    #endif
    var body: some Scene { WindowGroup { CollectorView() } }
}

enum AppConfiguration {
    static var dashboardURL: URL? {
        guard var parts = URLComponents(string: UserDefaults.standard.string(forKey: "ingestionURL") ?? ""),
              parts.scheme == "https", parts.host != nil, parts.user == nil, parts.password == nil,
              parts.path == "/v1/batches", parts.query == nil, parts.fragment == nil else { return nil }
        parts.path = "/d/life-whoop"; return parts.url
    }
    static let defaultEndpoint = Bundle.main.object(forInfoDictionaryKey: "LifeIngestionURL") as? String ?? ""
}

struct CollectorSettingsView: View {
    @ObservedObject var collector = Services.shared.collector
    @ObservedObject var uploader = Services.shared.uploader
    @AppStorage("ingestionURL") private var endpoint = AppConfiguration.defaultEndpoint
    @State private var token = ""
    @State private var settingsMessage = ""
    var body: some View {
        Form {
            Section("WHOOP connection") {
                Button("Find my WHOOP", action: collector.scan)
                ForEach(collector.devices, id: \.identifier) { device in
                    Button(device.name ?? "WHOOP · \(device.identifier.uuidString.prefix(8))") { collector.connect(device) }
                }
                if collector.isStopped {
                    Button("Resume collecting", action: collector.resume)
                } else { Button("Stop collecting", action: collector.stop) }
                Text(collector.status).foregroundStyle(.secondary)
            }
            Section("Device readings") {
                TimelineView(.periodic(from: .now, by: 30)) { context in
                    Text(collector.batteryEvidence.status(at: context.date))
                }
                if let date = collector.lastSeen {
                    LabeledContent("Last strap data") { Text(date, style: .relative) }
                }
            }
            Section("Stored readings") {
                Text(collector.historyStatus)
                if collector.historyRecords > 0 { LabeledContent("Records this sync", value: String(collector.historyRecords)) }
                if let date = collector.historyThrough { LabeledContent("Latest recovered reading") { Text(date, format: .dateTime.month().day().hour().minute()) } }
                Button("Sync stored readings", action: collector.syncStoredReadings)
            }
            Section("Sensor research") {
                Text(collector.researchStatus).font(.caption)
                if collector.researchRunning {
                    Button("Finish capture", action: collector.finishSensorResearch)
                } else {
                    Button("Capture additional sensors", action: collector.startSensorResearch)
                    Button("Stop extra sensor streams", action: collector.finishSensorResearch)
                }
                Text("Keep Life open for 65 seconds. Records motion, optical data and one history batch. Stored history is left intact.")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(collector.sensorFrames.keys.sorted(), id: \.self) { name in
                    LabeledContent(name, value: String(collector.sensorFrames[name] ?? 0))
                }
            }
            Section("Remote backend") {
                TextField("https://your-host/v1/batches", text: $endpoint)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    #endif
                SecureField("Ingestion token", text: $token)
                Button("Save token") {
                    do { try Credentials.save(token); token = ""; settingsMessage = "Token stored in Keychain"; uploader.sync(force: true) }
                    catch { settingsMessage = "Use the full ingestion token from your Life installation" }
                }
                if !settingsMessage.isEmpty { Text(settingsMessage).font(.caption) }
            }
            Section("Reminders") {
                Text("Recharge alerts at 20% and 10% after battery cross-checks. A separate reminder appears after an hour without strap data.")
                Button("Enable notifications", action: collector.enableAlerts)
            }
            Section("Validation") {
                Text("Stored readings are saved locally before the strap advances its download cursor. HRV uses quality-screened pulse timing. Sleep times are self-reported.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .scrollContentBackground(.hidden)
        .background(LifeTheme.canvas)
        .tint(LifeTheme.mint)
        .preferredColorScheme(.dark)
        .navigationTitle("Settings")
    }
}
