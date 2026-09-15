import XCTest
import Combine
import WhoopCore
@testable import WhoopCollector

final class UploaderTests: XCTestCase {
    @MainActor
    func testNewCapturesReplaceIdleSuccessDuringBatchDelay() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let suite = UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite); try? FileManager.default.removeItem(at: root) }
        defaults.set("https://example.invalid/v1/batches", forKey: "ingestionURL")
        let store = try CaptureStore(url: root.appendingPathComponent("captures.sqlite"))
        let uploader = Uploader(store: store, directory: root, defaults: defaults,
                                readToken: { String(repeating: "x", count: 64) }, configuration: .ephemeral)
        // Complete the empty-queue check, then receive a BLE capture inside the 60-second delay.
        let checked = expectation(description: "initial empty queue checked")
        let observation = uploader.$status.filter { $0 == "All captured data saved on backend" || $0 == "No captured data yet" }
            .prefix(1).sink { _ in checked.fulfill() }
        uploader.sync()
        await fulfillment(of: [checked], timeout: 3)
        observation.cancel()
        XCTAssertEqual(uploader.status, "No captured data yet")
        let capture = Capture(deviceId: "strap", sessionId: "test", source: "live_hr", raw: Data([0, 75]))
        try store.insert(capture)
        uploader.sync()
        XCTAssertEqual(uploader.pendingCount, 1)
        XCTAssertEqual(uploader.status, "Waiting to upload to backend")
        XCTAssertNil(uploader.lastAcknowledged)
        try store.acknowledge([capture.id])
        uploader.refreshStatus()
        XCTAssertEqual(uploader.status, "All captured data saved on backend")
    }
}
