import XCTest
@testable import WhoopCollector

final class SleepHistoryTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    func testNewPastEntryRequiresBothTimesAndKeepsStableIdentity() throws {
        var draft = SleepDraft()
        XCTAssertNil(draft.session(now: now, sessions: []))
        draft.start = now.addingTimeInterval(-7 * 86400 - 8 * 3600)
        XCTAssertNil(draft.session(now: now, sessions: []))
        draft.end = now.addingTimeInterval(-7 * 86400)
        draft.awakeMinutes = "30"
        let row = try XCTUnwrap(draft.session(now: now, sessions: []))
        XCTAssertEqual(row.revision, 1)
        XCTAssertEqual(row.durationSeconds, 7.5 * 3600)
        XCTAssertEqual(row.id, draft.session(now: now, sessions: [])?.id)
    }
    func testEditingAnOlderEntryPreservesIDAndBumpsRevision() throws {
        let old = SleepSession(id: "old-entry", revision: 4, start: now.timeIntervalSince1970-14*86400-8*3600,
                               end: now.timeIntervalSince1970-14*86400, awake_seconds: 0)
        var draft = SleepDraft(row: old)
        XCTAssertFalse(draft.hasChanges)
        draft.awakeMinutes = "20"
        let edited = try XCTUnwrap(draft.session(now: now, sessions: [old]))
        XCTAssertEqual(edited.id, old.id); XCTAssertEqual(edited.revision, 5)
        XCTAssertEqual(edited.awake_seconds, 1200); XCTAssertTrue(draft.hasChanges)
        var newer = old; newer.revision += 1
        XCTAssertNil(draft.session(now: now, sessions: [newer]))
    }
    func testInvalidDatesAwakeTimeAndOverlapsCannotBeSaved() {
        var draft = SleepDraft()
        draft.start = now.addingTimeInterval(-8*3600); draft.end = now
        XCTAssertNotNil(draft.session(now: now, sessions: []))
        for value in ["-1", "480", "no", "", "nan", "inf"] {
            draft.awakeMinutes = value
            XCTAssertNil(draft.session(now: now, sessions: []), value)
        }
        draft.awakeMinutes = "0"
        draft.end = now.addingTimeInterval(60)
        XCTAssertNil(draft.session(now: now, sessions: []))
        draft.end = draft.start
        XCTAssertNil(draft.session(now: now, sessions: []))
        draft.end = now; draft.start = now.addingTimeInterval(-25*3600)
        XCTAssertNil(draft.session(now: now, sessions: []))
        draft.start = now.addingTimeInterval(-8*3600)
        let overlap = SleepSession(id: "existing", revision: 1, start: now.timeIntervalSince1970-3600, end: now.timeIntervalSince1970, awake_seconds: 0)
        XCTAssertNil(draft.session(now: now, sessions: [overlap]))
    }
    func testAdjacentPastEntriesAndCurrentSleepCanCoexist() {
        var draft = SleepDraft()
        draft.start = now.addingTimeInterval(-2*86400); draft.end = now.addingTimeInterval(-2*86400+3600)
        let next = SleepSession(id: "next", revision: 1, start: draft.end!.timeIntervalSince1970, end: draft.end!.timeIntervalSince1970+3600, awake_seconds: 0)
        let active = SleepSession(id: "active", revision: 1, start: now.timeIntervalSince1970-60, end: nil, awake_seconds: 0)
        XCTAssertNotNil(draft.session(now: now, sessions: [next, active]))
    }
    func testSleepAcrossDaylightSavingUsesElapsedTime() throws {
        let formatter = ISO8601DateFormatter()
        var draft = SleepDraft()
        draft.start = try XCTUnwrap(formatter.date(from: "2026-03-07T23:00:00-08:00"))
        draft.end = try XCTUnwrap(formatter.date(from: "2026-03-08T07:00:00-07:00"))
        XCTAssertEqual(draft.duration, 7*3600)
        draft.start = try XCTUnwrap(formatter.date(from: "2025-11-01T23:00:00-07:00"))
        draft.end = try XCTUnwrap(formatter.date(from: "2025-11-02T07:00:00-08:00"))
        XCTAssertEqual(draft.duration, 9*3600)
    }
}
