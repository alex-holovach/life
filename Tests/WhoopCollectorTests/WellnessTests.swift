import XCTest
@testable import WhoopCollector

final class WellnessTests: XCTestCase {
    func testOfflineEditsSurviveRoundTripAndOldAckCannotDropNewEdit() throws {
        let row = SleepSession(id: "synthetic-session", revision: 1, start: 1_800_000_000, end: nil, awake_seconds: 0)
        var journal = WellnessJournal()
        journal.sessions = [row]; journal.pending = [row.id]
        journal = try JSONDecoder().decode(WellnessJournal.self, from: JSONEncoder().encode(journal))
        XCTAssertEqual(journal.sessions, [row]); XCTAssertTrue(journal.pending.contains(row.id))
        journal.sessions[0].end = row.start + 3600; journal.sessions[0].revision = 2
        journal.acknowledge(row)
        XCTAssertTrue(journal.pending.contains(row.id))
        journal.merge([row]); XCTAssertEqual(journal.sessions[0].revision, 2)
        journal.acknowledge(journal.sessions[0]); XCTAssertTrue(journal.pending.isEmpty)
    }
    func testStaleOrFutureAnalysisIsUnavailable() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let profile = WellnessProfile()
        for (age, current) in [(0.0,true),(179,true),(180,false),(-1,false)] {
            let value = WellnessSnapshot(state: "ready", profile: profile, sessions: [], strain: nil, sleep: nil, computed_at: now.timeIntervalSince1970-age)
            XCTAssertEqual(value.current(at: now), current)
        }
    }
}
