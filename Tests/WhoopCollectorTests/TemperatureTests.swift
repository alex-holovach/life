import XCTest
@testable import WhoopCollector

final class TemperatureTests: XCTestCase {
    func testReadingExpiresAndUnknownFirmwareCannotShowDegrees() throws {
        let now = Date(timeIntervalSince1970: 1_789_360_000)
        let current = TemperatureSnapshot(state: "ready", celsius: 33.2, observed_at: now.timeIntervalSince1970 - 300, decoder: "v1", points: [])
        XCTAssertTrue(current.isCurrent(at: now))
        XCTAssertFalse(current.isCurrent(at: now.addingTimeInterval(301)))
        let pending = TemperatureSnapshot(state: "unsupported_firmware", celsius: 33.2, observed_at: now.timeIntervalSince1970, decoder: nil, points: [])
        XCTAssertFalse(pending.isCurrent(at: now))
        let future = TemperatureSnapshot(state: "ready", celsius: 33.2, observed_at: now.timeIntervalSince1970 + 1, decoder: "v1", points: [])
        XCTAssertFalse(future.isCurrent(at: now))
    }
}
