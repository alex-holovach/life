import Foundation

/// Deadlines use monotonic uptime. Call on Bluetooth callbacks and foreground entry;
/// a timer is supplementary because iOS can suspend it in the background.
public struct ConnectionRecovery {
    public enum Action: Equatable { case none, restartLive, reconnect(String) }
    private enum Stage { case idle, preparing, receiving }
    private var stage = Stage.idle
    private var began: TimeInterval = 0
    private var lastHeartRate: TimeInterval = 0
    private var restartedAt: TimeInterval?
    private var writingSince: TimeInterval?

    public init() {}
    public static func notificationsReady(_ channels: Set<String>) -> Bool {
        Set(["response", "data", "heartRate"]).isSubset(of: channels)
    }
    public mutating func connected(at now: TimeInterval) {
        self = Self(); stage = .preparing; began = now
    }
    public mutating func ready(at now: TimeInterval) { stage = .receiving; lastHeartRate = now }
    public mutating func receivedHeartRate(at now: TimeInterval) {
        // A late notification during rediscovery must not declare setup complete.
        guard stage == .receiving else { return }
        lastHeartRate = now; restartedAt = nil
    }
    public mutating func writeStarted(at now: TimeInterval) { writingSince = now }
    public mutating func writeFinished() { writingSince = nil }
    public mutating func disconnected() { self = Self() }
    public mutating func check(at now: TimeInterval) -> Action {
        guard stage != .idle else { return .none }
        if let start = writingSince, now - start >= 15 { return reconnect("write_timeout") }
        if stage == .preparing, now - began >= 30 { return reconnect("setup_timeout") }
        guard stage == .receiving else { return .none }
        if let restartedAt {
            if now - restartedAt >= 15 { return reconnect("telemetry_timeout") }
        } else if now - lastHeartRate >= 20 {
            restartedAt = now; return .restartLive
        }
        return .none
    }
    private mutating func reconnect(_ reason: String) -> Action {
        disconnected(); return .reconnect(reason)
    }
}
