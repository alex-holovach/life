import XCTest
@testable import WhoopCollector

final class VitalsTests: XCTestCase {
    let now = Date(timeIntervalSince1970: 1_800_000_000)

    func testOxygenRequiresSupportedDecoderFreshnessAndUnambiguousCode() {
        func oxygen(_ value: Double = 95, state: String = "ready", at: Double = 1_799_999_990,
                    decoder: String = "harvard_41_17_4_0_spo2_v1") -> VitalsSnapshot.Oxygen {
            .init(state: state, percent: value, observed_at: at, decoder: decoder)
        }
        XCTAssertTrue(oxygen().isCurrent(at: now))
        XCTAssertFalse(oxygen(98).isCurrent(at: now))
        XCTAssertFalse(oxygen(0).isCurrent(at: now))
        XCTAssertFalse(oxygen(128).isCurrent(at: now))
        XCTAssertFalse(oxygen(.nan).isCurrent(at: now))
        XCTAssertFalse(oxygen(state: "analysis_unavailable").isCurrent(at: now))
        XCTAssertFalse(oxygen(at: 1_800_000_001).isCurrent(at: now))
        XCTAssertFalse(oxygen().isCurrent(at: now.addingTimeInterval(86401)))
        XCTAssertFalse(oxygen(decoder: "unknown").isCurrent(at: now))
    }

    func testRespirationRequiresQualityCheckedFreshSegment() {
        func respiration(state: String = "ready", duration: Double = 120,
                         quality: String = "signal_checks_passed", coverage: Double = 1) -> VitalsSnapshot.Respiration {
            .init(state: state, breaths_per_minute: 12, end: 1_799_999_990,
                  algorithm: "r24_pulse_respiration_v1", quality: quality, duration_seconds: duration, coverage: coverage)
        }
        XCTAssertTrue(respiration().isCurrent(at: now))
        XCTAssertFalse(respiration(state: "insufficient_data").isCurrent(at: now))
        XCTAssertFalse(respiration(duration: 89).isCurrent(at: now))
        XCTAssertFalse(respiration(quality: "unavailable").isCurrent(at: now))
        XCTAssertFalse(respiration(coverage: 0.7).isCurrent(at: now))
        XCTAssertFalse(respiration().isCurrent(at: now.addingTimeInterval(901)))
        XCTAssertTrue(respiration(state: "stale").isAvailable(at: now.addingTimeInterval(3*3600)))
        XCTAssertFalse(respiration(state: "stale").isAvailable(at: now.addingTimeInterval(86400)))
        XCTAssertFalse(respiration(state: "analysis_unavailable").isAvailable(at: now))
        XCTAssertFalse(respiration(state: "stale", coverage: 0.7).isAvailable(at: now))
    }

    func testUnavailableBreathingExplainsDecodedSignalFailure() throws {
        let json = """
        {"state":"insufficient_data","algorithm":"r24_pulse_respiration_v1",
         "quality":"insufficient_signal","duration_seconds":3,"coverage":0,
         "reasons":["short_contiguous_segment"]}
        """
        let result = try JSONDecoder().decode(VitalsSnapshot.Respiration.self, from: Data(json.utf8))
        XCTAssertEqual(result.signalDetail, "No verified 90-second pulse segment. Empty batches can occur at slow heart rates.")
        XCTAssertFalse(result.isAvailable(at: now))
    }
}
