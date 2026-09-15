import Foundation
import CoreBluetooth
import UserNotifications
import Combine
import WhoopCore
#if os(iOS)
import UIKit
#endif

/// CoreBluetooth and the SQLite writer are confined to the main queue.
/// Chart reads use a separate read-only connection on a serial worker queue.
final class Collector: NSObject, ObservableObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    @Published var status = "Ready to pair"
    @Published var devices: [CBPeripheral] = []
    @Published private(set) var heartRateReadings: [HeartRateReading] = []
    @Published var heartRate: Int?
    @Published var batteryEvidence = BatteryEvidence()
    @Published var batteryReadAt: Date?
    @Published var lastSeen: Date?
    @Published var saved = 0
    @Published var error: String?
    @Published private(set) var researchStatus = "Ready for a 65-second sensor capture"
    @Published private(set) var researchRunning = false
    @Published private(set) var sensorFrames: [String: Int] = [:]
    private var researchTasks: [DispatchWorkItem] = []
    private var researchCleanupSequence: UInt8?
    private var researchLaunchUsed = false
    @Published private(set) var historyStatus = "Waiting to sync stored readings"
    @Published private(set) var historyRecords = 0
    @Published private(set) var historyThrough: Date?
    private var historyTransfer = HistoryTransfer()
    private var historyRecovery = HistoryRecovery()
    private var clockValidation = ClockValidation()
    private var clockVerified: Bool { clockValidation.verified }
    private var clockRepairSent = false
    private var lastClockRequest = Date.distantPast
    private var lastChartRefresh = Date.distantPast
    private let chartQueue = DispatchQueue(label: "life.chart-reader", qos: .utility)
    private var chartRefreshInProgress = false
    private var lastSlowChartReport: TimeInterval = 0
    private var lastSlowCallbackReport: TimeInterval = 0
    @Published private(set) var isStopped = UserDefaults.standard.bool(forKey: "collectionStopped")
    let store: CaptureStore
    let directory: URL
    var onSaved: (() -> Void)?
    private var central: CBCentralManager!
    private var strap: CBPeripheral?
    private var restoredNeedsPreparation = false
    private var commands: CBCharacteristic?
    private var batteryCharacteristic: CBCharacteristic?
    private var writeQueue: [Data] = []
    private var writeInFlight = false
    private var currentWrite: Data?
    private var sequence: UInt8 = 0
    private var decoder = NotificationDecoder()
    private var sessionId = UUID().uuidString
    private var bondComplete = false
    private var shouldReconnect: Bool { !isStopped }
    private var subscribedCustom: Set<CBUUID> = []
    private var startedLive = false
    private var lastBatteryRequest = Date.distantPast
    private var lastReminderUpdate = Date.distantPast
    private var lastHistoryRequest = Date.distantPast
    private var historyBlocked = false
    private var recovery = ConnectionRecovery()
    private var recoveryTimer: Timer?
    private var reconnectTask: DispatchWorkItem?
    private var connectionFailures = 0
    private var recovering = false
    #if DEBUG
    private var reliabilityTestStarted = false
    private var testReconnectAfter: TimeInterval?
    private var testTasks: [DispatchWorkItem] = []
    #endif
    private var uptime: TimeInterval { ProcessInfo.processInfo.systemUptime }
    private let custom = CBUUID(string: "61080001-8d6d-82b8-614a-1c8cb0f8dcc6")
    private let commandId = CBUUID(string: "61080002-8d6d-82b8-614a-1c8cb0f8dcc6")
    private let hrId = CBUUID(string: "2A37")
    private let batteryId = CBUUID(string: "2A19")

    override init() {
        do {
            directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                   appropriateFor: nil, create: true).appendingPathComponent("WhoopLocal")
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            #if os(iOS)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: directory.path)
            #endif
            store = try CaptureStore(url: directory.appendingPathComponent("captures.sqlite"))
        } catch { fatalError("Cannot open local capture storage: \(error)") }
        super.init()
        #if os(iOS)
        NotificationCenter.default.addObserver(self, selector: #selector(researchBackgrounded),
            name: UIApplication.willResignActiveNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(becameActive),
            name: UIApplication.didBecomeActiveNotification, object: nil)
        #endif
        refreshChart()
        if isStopped { status = "Stopped" }
        central = CBCentralManager(delegate: self, queue: .main,
            options: [CBCentralManagerOptionRestoreIdentifierKey: "whoop-local.central.v1"])
        recordEvent("app_launch")
        recoveryTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.checkRecovery() }
    }
    func scan() {
        guard central.state == .poweredOn else { error = "Enable Bluetooth in Settings."; return }
        error = nil; devices = []; status = "Scanning for WHOOP 4.0"
        central.scanForPeripherals(withServices: [custom])
    }
    func resume() {
        isStopped = false; UserDefaults.standard.set(false, forKey: "collectionStopped")
        if let strap, central.state == .poweredOn { connect(strap) }
        else { centralManagerDidUpdateState(central); if strap == nil { scan() } }
    }
    func connect(_ device: CBPeripheral) {
        isStopped = false; UserDefaults.standard.set(false, forKey: "collectionStopped")
        if strap?.identifier == device.identifier, device.state == .connected {
            if !startedLive { prepare(device) } else { checkRecovery() }
            return
        }
        if let old = strap, old.identifier != device.identifier { central.cancelPeripheralConnection(old) }
        central.stopScan(); devices = []; strap = device; device.delegate = self; error = nil
        UserDefaults.standard.set(device.identifier.uuidString, forKey: "strapId")
        beginConnection(device)
    }
    func stop() {
        isStopped = true; UserDefaults.standard.set(true, forKey: "collectionStopped"); central.stopScan()
        reconnectTask?.cancel(); reconnectTask = nil; recovery.disconnected()
        recordEvent("user_stopped")
        writeQueue = []
        historyTransfer.invalidate(); historyStatus = "Collection stopped"
        #if DEBUG
        testTasks.forEach { $0.cancel() }; testTasks = []; testReconnectAfter = nil
        #if os(iOS)
        UIApplication.shared.isIdleTimerDisabled = false
        #endif
        #endif
        if let strap { central.cancelPeripheralConnection(strap) }
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ["whoop-stale"])
        status = "Stopped"
    }
    func enableAlerts() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, failure in
            DispatchQueue.main.async {
                if !granted { self.error = failure?.localizedDescription ?? "Enable notifications in iPhone Settings to receive recharge reminders." }
                else if let strap = self.strap {
                    UserDefaults.standard.removeObject(forKey: "batteryAlerts.\(strap.identifier.uuidString)")
                    if self.shouldReconnect { self.refreshBattery(strap, now: Date()) }
                }
            }
        }
    }
    private func beginConnection(_ peripheral: CBPeripheral) {
        guard shouldReconnect, central.state == .poweredOn else { return }
        #if DEBUG
        if let due = testReconnectAfter, uptime < due {
            reconnectTask?.cancel()
            let task = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.testReconnectAfter = nil; self.recordEvent("test_gap_end"); self.beginConnection(peripheral)
            }
            reconnectTask = task
            DispatchQueue.main.asyncAfter(deadline: .now() + (due - uptime), execute: task)
            return
        }
        #endif
        reconnectTask?.cancel(); reconnectTask = nil
        if peripheral.state == .connected { prepare(peripheral); return }
        if peripheral.state == .connecting { return }
        // CoreBluetooth keeps this request pending through an out-of-range period,
        // including when iOS suspends the app. Do not repeatedly cancel it on a timer.
        peripheral.delegate = self
        central.connect(peripheral); status = "Waiting for WHOOP"
        recordEvent("connection_requested")
    }
    private func reconnect(reason: String) {
        guard shouldReconnect, !recovering, let strap, central.state == .poweredOn else { return }
        recovering = true; recovery.disconnected()
        recordEvent("recovery_reconnect", detail: reason)
        status = "Recovering connection"; heartRate = nil
        writeQueue = []; writeInFlight = false; currentWrite = nil
        if strap.state == .disconnected { beginConnection(strap) }
        else { central.cancelPeripheralConnection(strap) }
    }
    private func checkRecovery() {
        guard shouldReconnect, central.state == .poweredOn, let strap, strap.state == .connected, !recovering else { return }
        switch recovery.check(at: uptime) {
        case .none: break
        case .restartLive:
            recordEvent("recovery_restart_stream")
            status = "Restarting live readings"; heartRate = nil
            if let hr = strap.services?.flatMap({ $0.characteristics ?? [] }).first(where: { $0.uuid == hrId }) {
                strap.setNotifyValue(true, for: hr)
            }
            enqueue(3, [1])
        case .reconnect(let reason): reconnect(reason: reason)
        }
        handleHistory(historyTransfer.check(at: uptime))
        if historyRecovery.isDue(at: uptime) {
            // A fresh GATT session excludes late packets from the aborted batch.
            // Its unacknowledged records remain on the strap for retransmission.
            reconnect(reason: "history_retry"); return
        }
        if startedLive, !clockVerified, !historyBlocked, !researchRunning,
           Date().timeIntervalSince(lastClockRequest) >= 10 {
            lastClockRequest = Date(); enqueue(11, [0])
        }
        if startedLive, clockVerified, !historyTransfer.running, Date().timeIntervalSince(lastHistoryRequest) >= 300 {
            requestHistory()
        }
    }
    #if os(iOS)
    @objc private func becameActive() {
        recordEvent("foreground")
        if shouldReconnect { centralManagerDidUpdateState(central); checkRecovery() }
    }
    #endif
    private func recordEvent(_ event: String, detail: String? = nil) {
        guard let id = strap?.identifier.uuidString ?? UserDefaults.standard.string(forKey: "strapId") else { return }
        var fields = ["event": event, "uptime": String(uptime)]
        if let detail { fields["detail"] = detail }
        #if os(iOS)
        fields["appState"] = String(UIApplication.shared.applicationState.rawValue)
        #endif
        do {
            let raw = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
            let row = Capture(deviceId: id, sessionId: sessionId, source: "collector_event", raw: raw)
            try save(row)
        } catch { self.error = "Cannot save connection event: \(error.localizedDescription)" }
    }
    func peripheral(_ peripheral: CBPeripheral, didModifyServices invalidatedServices: [CBService]) {
        guard strap?.identifier == peripheral.identifier else { return }
        reconnect(reason: "services_invalidated")
    }
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        recordEvent("bluetooth_state", detail: String(central.state.rawValue))
        guard shouldReconnect else {
            if central.state == .poweredOn, let strap, strap.state != .disconnected {
                central.cancelPeripheralConnection(strap)
            }
            status = "Stopped"; return
        }
        guard central.state == .poweredOn else {
            recovery.disconnected(); startedLive = false; restoredNeedsPreparation = true
            status = "Bluetooth unavailable"; return
        }
        if let strap, strap.state == .connected, restoredNeedsPreparation { prepare(strap); return }
        if let strap, strap.state == .disconnected { beginConnection(strap); return }
        if strap == nil, let id = UserDefaults.standard.string(forKey: "strapId"), let uuid = UUID(uuidString: id),
           let device = central.retrievePeripherals(withIdentifiers: [uuid]).first { connect(device); return }
        if strap == nil, UserDefaults.standard.string(forKey: "strapId") != nil { scan() }
    }
    func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        if let restored = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral])?.first {
            strap = restored; restored.delegate = self
            // Restoration precedes the powered-on callback. Retain the peripheral now,
            // then rediscover/query (or cancel a stopped connection) only when Bluetooth is ready.
            restoredNeedsPreparation = true
            recordEvent("state_restored", detail: String(restored.state.rawValue))
            if central.state == .poweredOn { centralManagerDidUpdateState(central) }
        }
    }
    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        if !devices.contains(where: { $0.identifier == peripheral.identifier }) { devices.append(peripheral) }
        if shouldReconnect, peripheral.identifier.uuidString == UserDefaults.standard.string(forKey: "strapId"),
           strap == nil || strap?.state == .disconnected { connect(peripheral) }
    }
    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard shouldReconnect, strap?.identifier == peripheral.identifier else { central.cancelPeripheralConnection(peripheral); return }
        prepare(peripheral)
    }
    private func prepare(_ peripheral: CBPeripheral) {
        cancelResearchTimers()
        researchRunning = false
        recovering = false; reconnectTask?.cancel(); reconnectTask = nil
        recovery.connected(at: uptime)
        historyTransfer.invalidate(); historyStatus = "Checking strap clock"
        historyRecovery.connected()
        clockValidation = ClockValidation(); clockRepairSent = false; lastClockRequest = .distantPast
        currentWrite = nil; researchCleanupSequence = nil
        restoredNeedsPreparation = false
        sessionId = UUID().uuidString; decoder = NotificationDecoder(); commands = nil; batteryCharacteristic = nil
        recordEvent("connected")
        bondComplete = false; writeQueue = []; writeInFlight = false; historyBlocked = false
        lastHistoryRequest = .distantPast; lastBatteryRequest = .distantPast
        subscribedCustom = []; startedLive = false; batteryEvidence = BatteryEvidence(); batteryReadAt = nil
        lastSeen = nil; heartRate = nil; error = nil
        status = "Discovering services"; peripheral.delegate = self
        // Inventory every advertised GATT service. Only known streams are subscribed;
        // unknown services/characteristics are recorded without writing commands to them.
        peripheral.discoverServices(nil)
    }
    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        guard shouldReconnect, strap?.identifier == peripheral.identifier else { return }
        status = "Connection failed; retrying"; self.error = error?.localizedDescription
        recovery.disconnected(); recordEvent("connect_failed", detail: error?.localizedDescription)
        connectionFailures += 1
        reconnectTask?.cancel()
        let task = DispatchWorkItem { [weak self] in
            guard let self, self.shouldReconnect, self.strap?.identifier == peripheral.identifier,
                  peripheral.state == .disconnected, self.central.state == .poweredOn else { return }
            self.beginConnection(peripheral)
        }
        reconnectTask = task
        DispatchQueue.main.asyncAfter(deadline: .now() + min(60, pow(2, Double(min(connectionFailures, 6)))), execute: task)
    }
    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        guard strap?.identifier == peripheral.identifier else { return }
        recordEvent("disconnected", detail: error?.localizedDescription)
        recovery.disconnected(); startedLive = false; writeQueue = []; writeInFlight = false; currentWrite = nil
        heartRate = nil
        historyTransfer.invalidate(); historyStatus = "Waiting for WHOOP to reconnect"
        if researchRunning { cancelResearchTimers(); researchRunning = false; researchStatus = "Capture interrupted; cleanup pending on reconnect" }
        status = shouldReconnect ? "Disconnected; waiting for strap" : "Stopped"
        if shouldReconnect, central.state == .poweredOn { beginConnection(peripheral) }
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard shouldReconnect, strap?.identifier == peripheral.identifier else { return }
        if let error { reconnect(reason: "service_discovery: \(error.localizedDescription)"); return }
        for service in peripheral.services ?? [] { peripheral.discoverCharacteristics(nil, for: service) }
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard shouldReconnect, strap?.identifier == peripheral.identifier else { return }
        if let error {
            recordEvent("characteristic_discovery_failed", detail: "\(service.uuid): \(error.localizedDescription)")
            if service.uuid == custom || service.uuid == CBUUID(string: "180D") { reconnect(reason: "characteristic_discovery") }
            return
        }
        for characteristic in service.characteristics ?? [] {
            let inventory = "service=\(service.uuid.uuidString);characteristic=\(characteristic.uuid.uuidString);properties=\(characteristic.properties.rawValue)"
            let row = Capture(deviceId: peripheral.identifier.uuidString, sessionId: sessionId,
                source: "gatt_inventory", raw: Data(inventory.utf8))
            do { try save(row) } catch { self.error = "Cannot save sensor inventory"; stop(); return }
            // Optional standard Device Information, Health Thermometer and Pulse
            // Oximeter services, if exposed by this firmware. Preserve their bytes;
            // their presence is not a claim that WHOOP implements these services.
            if ["180A", "1809", "1822"].contains(service.uuid.uuidString.uppercased()) {
                if characteristic.properties.contains(.read) { peripheral.readValue(for: characteristic) }
                if characteristic.properties.contains(.notify) || characteristic.properties.contains(.indicate) {
                    peripheral.setNotifyValue(true, for: characteristic)
                }
            }
            if characteristic.uuid == commandId { commands = characteristic }
            if characteristic.uuid == batteryId {
                batteryCharacteristic = characteristic
                if characteristic.properties.contains(.read) { peripheral.readValue(for: characteristic); lastBatteryRequest = Date() }
            }
            if characteristic.uuid == hrId || characteristic.uuid == batteryId {
                if characteristic.isNotifying { subscribedCustom.insert(characteristic.uuid) }
                if characteristic.properties.contains(.notify) || characteristic.properties.contains(.indicate) {
                    peripheral.setNotifyValue(true, for: characteristic)
                }
            }
        }
        if service.uuid == custom, commands != nil { enqueue(26, [0]) } // Confirmed battery query triggers bonding.
    }
    private func enqueue(_ command: UInt8, _ payload: [UInt8] = []) {
        guard shouldReconnect else { return }
        guard command != 22 || HistorySafety.requestsEnabled || researchRunning,
              command != 23 || (HistorySafety.acknowledgementsEnabled && historyTransfer.running) else { return }
        sequence &+= 1; writeQueue.append(WhoopWire.command(command, sequence: sequence, payload: payload)); writeNext()
    }
    private func writeNext() {
        guard !writeInFlight, !writeQueue.isEmpty, let commands, let strap, strap.state == .connected else { return }
        guard commands.properties.contains(.write) else { error = "Confirmed command writes unavailable."; return }
        var bytes = writeQueue.removeFirst()
        if bytes[6] == 10 {
            // Build the RTC value at the actual write, not when queued behind setup.
            bytes = WhoopWire.command(10, sequence: bytes[5], payload:
                WhoopWire.littleEndian(UInt32(Date().timeIntervalSince1970)) + [0, 0, 0, 0, 0])
        }
        if bytes[6] == 11, !researchRunning {
            lastClockRequest = Date()
            clockValidation.request(sequence: bytes[5], wall: lastClockRequest.timeIntervalSince1970, uptime: uptime)
        }
        currentWrite = bytes; writeInFlight = true
        recovery.writeStarted(at: uptime)
        strap.writeValue(bytes, for: commands, type: .withResponse)
    }
    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        guard shouldReconnect, strap?.identifier == peripheral.identifier else { return }
        guard characteristic === commands, currentWrite != nil else { return }
        recovery.writeFinished()
        writeInFlight = false
        let completedSequence = currentWrite?[5]; currentWrite = nil
        if let error {
            self.error = error.localizedDescription; historyBlocked = true; writeQueue = []
            if researchRunning { cancelResearchTimers(); researchRunning = false; researchStatus = "Command failed; cleanup pending on reconnect" }
            reconnect(reason: "command_write: \(error.localizedDescription)")
            return
        }
        if let cleanup = researchCleanupSequence, completedSequence == cleanup {
            researchCleanupSequence = nil
            UserDefaults.standard.set(false, forKey: "sensorProbeCleanupRequired")
            researchStatus = "Capture saved; normal collection resumed"
        }
        if !bondComplete {
            bondComplete = true
            for service in peripheral.services ?? [] where service.uuid == custom {
                for c in service.characteristics ?? [] where c.properties.contains(.notify) || c.properties.contains(.indicate) {
                    if c.isNotifying { subscribedCustom.insert(c.uuid) }
                    peripheral.setNotifyValue(true, for: c)
                }
            }
        }
        startLiveIfReady(peripheral)
        writeNext()
    }
    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard shouldReconnect, strap?.identifier == peripheral.identifier else { return }
        if let error {
            recordEvent("subscription_failed", detail: "\(characteristic.uuid): \(error.localizedDescription)")
            if [hrId, CBUUID(string: "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"), CBUUID(string: "61080005-8d6d-82b8-614a-1c8cb0f8dcc6")].contains(characteristic.uuid) {
                reconnect(reason: "required_subscription")
            }
            return
        }
        if characteristic.isNotifying {
            subscribedCustom.insert(characteristic.uuid)
        } else { subscribedCustom.remove(characteristic.uuid) }
        startLiveIfReady(peripheral)
    }
    private func startLiveIfReady(_ peripheral: CBPeripheral) {
        let roles = Set(subscribedCustom.compactMap { id -> String? in
            switch id.uuidString.lowercased() {
            case "2a37": return "heartRate"
            case "61080003-8d6d-82b8-614a-1c8cb0f8dcc6": return "response"
            case "61080005-8d6d-82b8-614a-1c8cb0f8dcc6": return "data"
            default: return nil
            }
        })
            if bondComplete, !startedLive, ConnectionRecovery.notificationsReady(roles) {
                startedLive = true
                recovery.ready(at: uptime); recordEvent("notifications_ready")
                enqueue(35, [0]); enqueue(7, [0]); enqueue(3, [1])
                // Query again after response notifications are confirmed, not merely requested.
                refreshBattery(peripheral, now: Date())
                status = "Connected; waiting for telemetry"
                if UserDefaults.standard.bool(forKey: "sensorProbeCleanupRequired") { finishSensorResearch() }
                // Explicit developer test launch only. Normal launches never enable raw streams.
                if !researchLaunchUsed, ProcessInfo.processInfo.arguments.contains("--sensor-research") {
                    researchLaunchUsed = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.startSensorResearch() }
                }
                #if DEBUG
                if !reliabilityTestStarted, ProcessInfo.processInfo.arguments.contains("--reliability-test") || ProcessInfo.processInfo.arguments.contains("--notification-recovery-test") {
                    startReliabilityTest()
                }
                #endif
            }
    }
    #if DEBUG
    /// Explicit developer launch only. Exercise real BLE callbacks and compare the
    /// resulting live gap with downloaded history; no fake samples are generated.
    private func startReliabilityTest() {
        reliabilityTestStarted = true
        #if os(iOS)
        UIApplication.shared.isIdleTimerDisabled = true
        #endif
        recordEvent("test_started")
        let notificationOnly = ProcessInfo.processInfo.arguments.contains("--notification-recovery-test")
        testAfter(notificationOnly ? 10 : 30) { c in
            guard let strap = c.strap, let hr = strap.services?.flatMap({ $0.characteristics ?? [] }).first(where: { $0.uuid == c.hrId }) else { return }
            c.recordEvent("test_stop_live_notifications"); strap.setNotifyValue(false, for: hr)
        }
        if !notificationOnly { testAfter(90) { c in
            guard let strap = c.strap, strap.state == .connected else { return }
            c.recordEvent("test_gap_begin", detail: "90 seconds with Bluetooth disconnected")
            c.testReconnectAfter = c.uptime + 90
            c.central.cancelPeripheralConnection(strap)
        } }
        testAfter(notificationOnly ? 60 : 300) { c in
            c.recordEvent("test_finished")
            do { try c.store.snapshot(to: c.directory.appendingPathComponent("reliability-test.sqlite")) }
            catch { c.recordEvent("test_snapshot_failed", detail: error.localizedDescription) }
            #if os(iOS)
            UIApplication.shared.isIdleTimerDisabled = false
            #endif
        }
    }
    private func testAfter(_ seconds: Double, action: @escaping (Collector) -> Void) {
        let task = DispatchWorkItem { [weak self] in
            guard let self, self.shouldReconnect else { return }; action(self)
        }
        testTasks.append(task); DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: task)
    }
    #endif
    func requestHistory() {
        guard HistorySafety.requestsEnabled, shouldReconnect, startedLive, clockVerified,
              !historyTransfer.running, !historyBlocked, !researchRunning else { return }
        historyTransfer.begin(at: uptime); lastHistoryRequest = Date()
        historyStatus = "Downloading stored readings"; historyRecords = 0
        recordEvent("history_started"); enqueue(34, [0]); enqueue(22, [0])
    }
    func syncStoredReadings() {
        if historyBlocked { reconnect(reason: "retry_history_after_validation_failure") }
        else { requestHistory() }
    }
    private func handleHistory(_ action: HistoryTransfer.Action) {
        switch action {
        case .none: break
        case .ack(let payload):
            recordEvent("history_batch_saved", detail: "records=\(historyTransfer.records);token=\(Data(payload.dropFirst()).base64EncodedString())")
            enqueue(23, payload)
        case .complete:
            historyRecovery.completed()
            historyStatus = "Stored readings synced"
            recordEvent("history_complete", detail: "records=\(historyTransfer.records)")
            refreshChart()
        case .abort(let reason):
            if reason == "record_gap", let gap = historyTransfer.lastGap {
                recordEvent("history_gap", detail: "version=\(gap.version);expected=\(gap.expected);received=\(gap.received)")
            }
            historyTransfer.invalidate(); historyBlocked = true
            let retry = historyRecovery.failed(reason, at: uptime)
            historyStatus = retry ? "History interrupted; retrying automatically" : "History paused: \(reason)"
            enqueue(20, [0]); recordEvent("history_paused", detail: reason)
        }
    }
    private func observeClock(_ frame: Frame, now: Date) {
        guard !clockVerified, !historyBlocked, !researchRunning else { return }
        let action: ClockValidation.Action
        if let response = WhoopWire.clockResponse(frame) {
            action = clockValidation.response(sequence: response.sequence, device: response.time,
                                               wall: now.timeIntervalSince1970, uptime: uptime)
        } else if let deviceTime = WhoopWire.liveDeviceTime(frame) {
            action = clockValidation.observeLive(device: deviceTime, received: now.timeIntervalSince1970)
        } else { return }
        switch action {
        case .verified:
            recordEvent("clock_verified")
            requestHistory()
        case .correct:
            guard startedLive, !clockRepairSent, !historyTransfer.running else { return }
            clockRepairSent = true
            historyStatus = "Correcting strap clock"
            recordEvent("clock_repair", detail: "two_matched_clock_responses")
            enqueue(10, [0]); enqueue(11, [0])
        case .none: break
        }
    }
    private func refreshChart() {
        guard !chartRefreshInProgress else { return }
        chartRefreshInProgress = true
        let through = Date(); lastChartRefresh = through
        let url = directory.appendingPathComponent("captures.sqlite")
        chartQueue.async { [weak self] in
            let started = ProcessInfo.processInfo.systemUptime
            let result = Result {
                let reader = try CaptureStore(url: url, readOnly: true)
                return try reader.heartRateReadings(since: through.addingTimeInterval(-3600), through: through)
            }
            let elapsed = ProcessInfo.processInfo.systemUptime-started
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.chartRefreshInProgress = false
                switch result {
                case .success(let readings):
                    // The query's upper bound excludes live points received while
                    // it ran. Preserve those points when replacing the old snapshot.
                    let newer = self.heartRateReadings.filter { $0.at > through }
                    self.heartRateReadings = Array((readings + newer).suffix(7200))
                case .failure(let error):
                    self.error = "Cannot refresh saved readings: \(error.localizedDescription)"
                }
                if elapsed > 0.1, self.uptime-self.lastSlowChartReport > 60 {
                    self.lastSlowChartReport = self.uptime
                    self.recordEvent("chart_read_slow", detail: "milliseconds=\(Int(elapsed*1000))")
                }
            }
        }
    }
    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard shouldReconnect, strap?.identifier == peripheral.identifier else { return }
        guard error == nil, let data = characteristic.value else { self.error = error?.localizedDescription; return }
        let processingBegan = uptime
        defer {
            let elapsed = uptime-processingBegan
            if elapsed > 0.1, uptime-lastSlowCallbackReport > 60 {
                lastSlowCallbackReport = uptime
                recordEvent("ble_processing_slow", detail: "milliseconds=\(Int(elapsed*1000));bytes=\(data.count)")
            }
        }
        let now = Date(); lastSeen = now
        var capture = Capture(deviceId: peripheral.identifier.uuidString, sessionId: sessionId,
                              source: characteristic.uuid.uuidString, raw: data)
        do {
            if characteristic.uuid == hrId {
                capture.source = "live_hr"; capture.sampleAt = now.timeIntervalSince1970
                if let (hr, rr, contact) = try? WhoopWire.heartRateWords(data) {
                    recovery.receivedHeartRate(at: uptime); connectionFailures = 0
                    capture.hrBpm = hr; capture.intervalWords = rr; capture.intervalStatus = "units_and_timing_unverified"; heartRate = hr
                    status = hr > 0 ? "Collecting" : "Connected; sensor acquiring"
                    capture.quality = contact == false ? "off_wrist" : (hr == 0 ? "sensor_acquiring" : "live_receipt_timestamp")
                } else { capture.quality = "invalid_hr_packet" }
                try save(capture)
            } else if characteristic.uuid == batteryId {
                capture.source = "battery"; capture.sampleAt = now.timeIntervalSince1970
                if data.count == 1, let value = data.first, value <= 100 {
                    capture.batteryPercent = Double(value); capture.quality = "unverified_standard_battery"
                    batteryEvidence.observe(Double(value), custom: false, at: now); batteryReadAt = now
                }
                try save(capture)
                try saveVerifiedBattery(device: capture.deviceId, raw: data, now: now)
            } else {
                let previouslyDiscarded = decoder.discardedBytes
                let frames = decoder.append(data, characteristic: characteristic.uuid.uuidString)
                // Decode first, then commit the entire notification before the
                // transfer state machine can produce an ACK. Invalid and unknown
                // packets still retain their original fragment for reprocessing.
                var rows = [capture]
                var decoded: [(frame: Frame?, historical: Capture?)] = []
                for bytes in frames {
                    guard let frame = try? Frame(bytes) else { decoded.append((nil, nil)); continue }
                    var wire = Capture(deviceId: capture.deviceId, sessionId: sessionId,
                                       source: "wire_frame", raw: bytes, receivedAt: capture.receivedAt)
                    wire.decoderVersion = "whoop4-v4"; wire.quality = "crc_valid"
                    rows.append(wire)
                    var historical: Capture?
                    if frame.type == 0x2f {
                        var row = Capture(deviceId: capture.deviceId, sessionId: sessionId, source: "history", raw: bytes)
                        row.decoderVersion = "whoop4-v4"
                        row.useHistoricalIdentity(); WhoopWire.historical(frame, capture: &row)
                        rows.append(row); historical = row
                    }
                    decoded.append((frame, historical))
                }
                try save(rows)
                if decoder.discardedBytes > previouslyDiscarded {
                    if !historyBlocked { handleHistory(.abort("framing_mismatch")) }
                }
                for item in decoded {
                    guard let frame = item.frame else {
                        if !historyBlocked { handleHistory(.abort("invalid_checksum")) }
                        self.error = "Unrecognized checksum. Raw bytes saved; history acknowledgement paused."; continue
                    }
                    // Persistence already committed above, including any end marker.
                    let historyAction = try historyTransfer.accept(frame, at: uptime) {}
                    if let at = item.historical?.sampleAt {
                        historyThrough = Date(timeIntervalSince1970: at)
                        if historyTransfer.running { historyRecovery.progress(through: at) }
                    }
                    historyRecords = historyTransfer.records
                    if frame.type == 0x2f, now.timeIntervalSince(lastChartRefresh) >= 5 { refreshChart() }
                    handleHistory(historyAction)
                    observeClock(frame, now: now)
                    let name = String(format: "type %02X", frame.type)
                    sensorFrames[name, default: 0] += 1
                    if let percent = WhoopWire.customBattery(frame) {
                        batteryEvidence.observe(percent, custom: true, at: now); batteryReadAt = now
                        var reading = Capture(deviceId: capture.deviceId, sessionId: sessionId, source: "battery_custom", raw: frame.raw)
                        reading.batteryPercent = percent; reading.quality = "unverified_custom_battery"
                        try save(reading)
                        try saveVerifiedBattery(device: capture.deviceId, raw: frame.raw, now: now)
                    }
                    if historyTransfer.running, frame.type == 0x24, frame.command == 23, frame.payload.count >= 2, frame.payload[1] != 1 {
                        historyTransfer.invalidate(); handleHistory(.abort("ack_rejected"))
                    }
                }
            }
        } catch {
            historyBlocked = true; self.error = "Storage failure: \(error.localizedDescription)"; stop(); return
        }
        // These run opportunistically on BLE callbacks, not on an unreliable background timer.
        if now.timeIntervalSince(lastBatteryRequest) >= 300 { refreshBattery(peripheral, now: now) }
        checkRecovery()
        if now.timeIntervalSince(lastReminderUpdate) >= 60 {
            lastReminderUpdate = now
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["whoop-stale"])
            notify(id: "whoop-stale", title: "WHOOP data is overdue",
                   body: "No recent strap data. Check Bluetooth, range, and charge. This does not confirm an empty battery.", delay: 3600)
        }
    }
    func startSensorResearch() {
        guard !researchRunning, !historyTransfer.running, startedLive, strap?.state == .connected, !writeInFlight,
              researchCleanupSequence == nil, writeQueue.isEmpty else { researchStatus = "Wait for the strap connection, then retry"; return }
        #if os(iOS)
        guard UIApplication.shared.applicationState == .active else { return }
        UIApplication.shared.isIdleTimerDisabled = true
        #endif
        researchRunning = true; researchStatus = "Reading device clock and sensor configuration"
        UserDefaults.standard.set(true, forKey: "sensorProbeCleanupRequired")
        for command: UInt8 in [11, 34, 98, 40, 42, 44, 62] { researchCommand(command, [0]) }
        researchAfter(5) { c in
            c.researchStatus = "Reading one history batch; acknowledgements disabled"
            c.researchCommand(22, [0])
        }
        researchAfter(20) { c in c.researchCommand(20, [0]) }
        researchAfter(25) { c in
            c.researchStatus = "Capturing motion samples"
            c.researchCommand(63, [1]); c.researchCommand(81, [1]); c.researchCommand(106, [1])
        }
        researchAfter(40) { c in
            c.researchCommand(63, [0]); c.researchCommand(106, [0]); c.researchCommand(82, [1])
        }
        researchAfter(45) { c in
            c.researchStatus = "Capturing wrist-gated optical samples"
            c.researchCommand(63, [1]); c.researchCommand(107, [1, 1])
        }
        researchAfter(65) { c in c.finishSensorResearch() }
    }
    private func researchAfter(_ seconds: Double, _ action: @escaping (Collector) -> Void) {
        let task = DispatchWorkItem { [weak self] in
            guard let self, self.researchRunning else { return }; action(self)
        }
        researchTasks.append(task); DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: task)
    }
    private func cancelResearchTimers() {
        researchTasks.forEach { $0.cancel() }; researchTasks = []
        #if os(iOS)
        UIApplication.shared.isIdleTimerDisabled = false
        #endif
    }
    private func researchCommand(_ command: UInt8, _ payload: [UInt8]) {
        guard let strap else { return }
        // Archive the command intent alongside its later response. This is never an ACK/trim,
        // RTC write, persistent optical mode, firmware update or hardware calibration.
        guard [11, 20, 22, 34, 40, 42, 44, 62, 63, 81, 82, 98, 106, 107].contains(command) else { return }
        let raw = WhoopWire.command(command, sequence: sequence &+ 1, payload: payload)
        let row = Capture(deviceId: strap.identifier.uuidString, sessionId: sessionId, source: "research_command", raw: raw)
        do { try save(row) } catch { self.error = "Cannot save research command"; return }
        enqueue(command, payload)
    }
    func finishSensorResearch() {
        cancelResearchTimers(); researchRunning = false
        UserDefaults.standard.set(true, forKey: "sensorProbeCleanupRequired")
        guard shouldReconnect, strap?.state == .connected, startedLive else {
            researchStatus = "Extra streams will be disabled on the next connection"; return
        }
        researchStatus = "Stopping extra streams; returning to normal collection"
        // Discard any unsent enabling commands. Keep the in-flight write until its callback.
        writeQueue = []
        for (command, payload): (UInt8, [UInt8]) in [(20, [0]), (63, [0]), (106, [0]), (107, [1, 0]), (82, [1])] {
            researchCommand(command, payload)
        }
        enqueue(3, [1]); researchCleanupSequence = sequence
        // The flag is cleared only after all cleanup writes complete; a crash/disconnect
        // leaves it set so the next connection sends cleanup again.
    }
    #if os(iOS)
    @objc private func researchBackgrounded() {
        recordEvent("leaving_foreground")
        if researchRunning { finishSensorResearch() }
    }
    #endif
    private func refreshBattery(_ peripheral: CBPeripheral, now: Date) {
        lastBatteryRequest = now
        if let batteryCharacteristic, batteryCharacteristic.properties.contains(.read) { peripheral.readValue(for: batteryCharacteristic) }
        if startedLive { enqueue(26, [0]) }
    }
    private func saveVerifiedBattery(device: String, raw: Data, now: Date) throws {
        guard let verified = batteryEvidence.verified(at: now) else { return }
        var capture = Capture(deviceId: device, sessionId: sessionId, source: "battery_verified", raw: raw)
        capture.sampleAt = verified.at.timeIntervalSince1970
        capture.batteryPercent = verified.percent; capture.quality = "cross_checked"
        try save(capture)
        batteryAlert(Int(verified.percent.rounded(.down)), device: device)
    }
    private func save(_ capture: Capture) throws {
        try save([capture])
    }
    private func save(_ captures: [Capture]) throws {
        try store.insert(captures)
        for capture in captures {
            if capture.source == "live_hr", let reading = HeartRateReading(capture: capture) {
                heartRateReadings.append(reading)
                let cutoff = Date().addingTimeInterval(-3600)
                heartRateReadings.removeAll { $0.at < cutoff }
                if heartRateReadings.count > 7200 { heartRateReadings.removeFirst(heartRateReadings.count - 7200) }
            }
        }
        saved += captures.count; onSaved?()
    }
    private func batteryAlert(_ percent: Int, device: String) {
        let key = "batteryAlerts.\(device)"
        var state = UserDefaults.standard.data(forKey: key).flatMap { try? JSONDecoder().decode(BatteryAlerts.self, from: $0) } ?? BatteryAlerts()
        if let threshold = state.observe(percent) {
            notify(id: "whoop-battery-\(threshold)", title: "Recharge your WHOOP",
                   body: "Your strap reported \(percent)% battery. Attach the battery pack soon.", delay: 1)
        }
        UserDefaults.standard.set(try? JSONEncoder().encode(state), forKey: key)
        if percent >= 25 {
            let ids = ["whoop-battery-10", "whoop-battery-20"]
            UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ids)
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ids)
        }
    }
    private func notify(id: String, title: String, body: String, delay: TimeInterval) {
        let content = UNMutableNotificationContent(); content.title = title; content.body = body; content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: content,
            trigger: UNTimeIntervalNotificationTrigger(timeInterval: delay, repeats: false)))
    }
}
