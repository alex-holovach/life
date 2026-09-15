import XCTest
@testable import WhoopCollector

final class DataQualityTests: XCTestCase {
    func testAnalysisAvailabilityAndReportAgeControlDisplay() throws {
        let json = """
        {"state":"ready","computed_at":1800000000,
         "history":{"through":1799999900,"record_coverage":0.8,"records":70000,"empty_interval_records":500},
         "hrv":{"analyzed_windows":10,"qualified_windows":2,"qualified_seconds":360,"reasons":{},"points":[{"at":1799990000,"value":25}]},
         "respiration":{"analyzed_windows":10,"qualified_windows":0,"qualified_seconds":0,"reasons":{},"points":[]}}
        """
        let report = try JSONDecoder().decode(DataQualityReport.self, from: Data(json.utf8))
        XCTAssertTrue(report.isCurrent(at: Date(timeIntervalSince1970: 1_800_000_010)))
        XCTAssertFalse(report.isCurrent(at: Date(timeIntervalSince1970: 1_800_000_180)))
        XCTAssertFalse(report.isCurrent(at: Date(timeIntervalSince1970: 1_799_999_999)))
        let stopped = try JSONDecoder().decode(DataQualityReport.self, from: Data(#"{"state":"analysis_unavailable"}"#.utf8))
        XCTAssertFalse(stopped.isCurrent(at: Date(timeIntervalSince1970: 1_800_000_010)))
        XCTAssertEqual(report.hrv?.points.first?.at, 1_799_990_000)
    }
}
