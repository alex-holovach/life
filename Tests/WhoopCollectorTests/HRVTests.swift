import XCTest
@testable import WhoopCollector

final class HRVTests: XCTestCase {
    func testValueRequiresFreshQualityCheckedWindow() throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        func snapshot(state: String = "ready", value: Double = 20, end: Double = 1_799_999_990,
                      coverage: Double = 0.999, algorithm: String = "r24_rmssd_5m_v1") -> HRVSnapshot {
            HRVSnapshot(state: state, rmssd_ms: value, end: end, algorithm: algorithm,
                        coverage: coverage, pairs: 300, latest_attempt: nil)
        }
        XCTAssertTrue(snapshot().isCurrent(at: now))
        XCTAssertFalse(snapshot().isCurrent(at: now.addingTimeInterval(901)))
        XCTAssertFalse(snapshot(state: "insufficient_data").isCurrent(at: now))
        XCTAssertFalse(snapshot(end: 1_800_000_001).isCurrent(at: now))
        XCTAssertFalse(snapshot(coverage: 0.8).isCurrent(at: now))
        XCTAssertFalse(snapshot(algorithm: "unknown").isCurrent(at: now))
        XCTAssertFalse(snapshot(value: .nan).isCurrent(at: now))
        XCTAssertTrue(snapshot(state: "stale", end: 1_799_989_200).isAvailable(at: now))
        XCTAssertFalse(snapshot(state: "stale", end: 1_799_900_000).isAvailable(at: now))
        XCTAssertFalse(snapshot(state: "analysis_unavailable").isAvailable(at: now))
        XCTAssertFalse(snapshot(state: "stale", coverage: 0.8).isAvailable(at: now))
        XCTAssertFalse(snapshot(end: 1_800_000_001).isAvailable(at: now))
    }
}
