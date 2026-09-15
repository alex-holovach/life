import XCTest
@testable import WhoopCore

final class RecoveryTests: XCTestCase {
    func testOptionalNotificationFailureDoesNotBlockCollection() {
        XCTAssertTrue(ConnectionRecovery.notificationsReady(["response", "data", "heartRate"]))
        XCTAssertFalse(ConnectionRecovery.notificationsReady(["response", "heartRate", "diagnostics"]))
    }
    func testSilentConnectionGetsOneStreamRestartThenReconnect() {
        var r = ConnectionRecovery()
        r.connected(at: 0); r.ready(at: 1); r.receivedHeartRate(at: 2)
        XCTAssertEqual(r.check(at: 21), .none)
        XCTAssertEqual(r.check(at: 22), .restartLive)
        for time in 23...36 { XCTAssertEqual(r.check(at: Double(time)), .none) }
        XCTAssertEqual(r.check(at: 37), .reconnect("telemetry_timeout"))
        XCTAssertEqual(r.check(at: 60), .none)
    }
    func testResumedReadingsCancelRecoveryEvenWithNoHeartRateAcquired() {
        var r = ConnectionRecovery()
        r.connected(at: 0); r.ready(at: 0)
        XCTAssertEqual(r.check(at: 20), .restartLive)
        // Receipt of a parsed 0 BPM / off-wrist reading still proves the stream is alive.
        r.receivedHeartRate(at: 24)
        XCTAssertEqual(r.check(at: 38), .none)
        r.disconnected()
        XCTAssertEqual(r.check(at: 400), .none, "OS-owned pending connections should be left pending")
    }
    func testLostWriteCallbackAndIncompleteSetupCannotWaitForever() {
        var r = ConnectionRecovery()
        r.connected(at: 0); r.writeStarted(at: 2)
        XCTAssertEqual(r.check(at: 16), .none)
        XCTAssertEqual(r.check(at: 17), .reconnect("write_timeout"))
        r.connected(at: 20); r.writeStarted(at: 21); r.writeFinished()
        XCTAssertEqual(r.check(at: 49), .none)
        XCTAssertEqual(r.check(at: 50), .reconnect("setup_timeout"))
    }
}
