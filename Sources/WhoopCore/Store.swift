import Foundation
import SQLite3

public final class CaptureStore {
    private var db: OpaquePointer?
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    public init(url: URL, readOnly: Bool = false) throws {
        let flags = readOnly ? SQLITE_OPEN_READONLY : SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE
        guard sqlite3_open_v2(url.path, &db, flags | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else { throw failure() }
        if readOnly { try execute("PRAGMA busy_timeout=5000"); return }
        try execute("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;")
        try execute("CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY, body TEXT NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0);")
        try execute("CREATE INDEX IF NOT EXISTS capture_hr_time ON captures(json_extract(body,'$.receivedAt')) WHERE json_extract(body,'$.source')='live_hr';")
        try execute("CREATE INDEX IF NOT EXISTS capture_sample_time ON captures(coalesce(json_extract(body,'$.sampleAt'),json_extract(body,'$.receivedAt'))) WHERE json_extract(body,'$.source') IN ('live_hr','history');")
        try execute("CREATE INDEX IF NOT EXISTS pending_captures ON captures(uploaded); CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value REAL NOT NULL);")
        try execute("""
            INSERT OR IGNORE INTO metadata(key,value) SELECT 'pendingCount',count(*) FROM captures WHERE uploaded=0;
            CREATE TRIGGER IF NOT EXISTS capture_pending_insert AFTER INSERT ON captures WHEN NEW.uploaded=0 BEGIN
                UPDATE metadata SET value=value+1 WHERE key='pendingCount';
            END;
            CREATE TRIGGER IF NOT EXISTS capture_pending_ack AFTER UPDATE OF uploaded ON captures WHEN OLD.uploaded=0 AND NEW.uploaded=1 BEGIN
                UPDATE metadata SET value=value-1 WHERE key='pendingCount';
            END;
            """)
    }
    deinit { sqlite3_close(db) }
    /// Consistent diagnostics while capture continues. Copying a live SQLite file
    /// and its WAL separately can produce an inconsistent copy.
    public func snapshot(to url: URL) throws {
        var output: OpaquePointer?
        guard sqlite3_open(url.path, &output) == SQLITE_OK else { throw failure() }
        defer { sqlite3_close(output) }
        guard let backup = sqlite3_backup_init(output, "main", db, "main") else { throw failure() }
        let result = sqlite3_backup_step(backup, -1)
        let finished = sqlite3_backup_finish(backup)
        guard result == SQLITE_DONE, finished == SQLITE_OK else { throw failure() }
        // The completed destination is standalone even when the source uses WAL.
        guard sqlite3_exec(output, "PRAGMA journal_mode=DELETE", nil, nil, nil) == SQLITE_OK else { throw failure() }
    }
    private func failure() -> NSError {
        NSError(domain: "SQLite", code: Int(sqlite3_errcode(db)), userInfo: [NSLocalizedDescriptionKey: String(cString: sqlite3_errmsg(db))])
    }
    private func execute(_ sql: String) throws {
        guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { throw failure() }
    }
    public func insert(_ capture: Capture) throws {
        try insert([capture])
    }
    /// Commit one notification's raw fragment, wire frames and decoded records
    /// atomically. Returning successfully means every row is durable under FULL.
    public func insert(_ captures: [Capture]) throws {
        guard !captures.isEmpty else { return }
        try execute("BEGIN IMMEDIATE")
        do {
            var statement: OpaquePointer?
            guard sqlite3_prepare_v2(db, "INSERT OR IGNORE INTO captures(id,body) VALUES(?,?)", -1, &statement, nil) == SQLITE_OK else { throw failure() }
            defer { sqlite3_finalize(statement) }
            for capture in captures {
                let body = String(decoding: try JSONEncoder().encode(capture), as: UTF8.self)
                sqlite3_reset(statement); sqlite3_clear_bindings(statement)
                sqlite3_bind_text(statement, 1, capture.id, -1, transient)
                sqlite3_bind_text(statement, 2, body, -1, transient)
                guard sqlite3_step(statement) == SQLITE_DONE else { throw failure() }
            }
            try execute("COMMIT")
        } catch { try? execute("ROLLBACK"); throw error }
    }
    public func pending(limit: Int = 500) throws -> [Capture] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT body FROM captures WHERE uploaded=0 ORDER BY rowid LIMIT ?", -1, &statement, nil) == SQLITE_OK else { throw failure() }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_int(statement, 1, Int32(limit))
        var rows: [Capture] = []; var status = sqlite3_step(statement)
        while status == SQLITE_ROW {
            let body = String(cString: sqlite3_column_text(statement, 0))
            rows.append(try JSONDecoder().decode(Capture.self, from: Data(body.utf8)))
            status = sqlite3_step(statement)
        }
        guard status == SQLITE_DONE else { throw failure() }; return rows
    }
    /// Includes acknowledged records: uploading never removes the local chart history.
    public func heartRateReadings(since: Date, through: Date = Date(), limit: Int = 7200) throws -> [HeartRateReading] {
        var statement: OpaquePointer?
        let sql = """
            SELECT json_extract(body,'$.id'), json_extract(body,'$.source'), json_extract(body,'$.quality'),
                   json_extract(body,'$.receivedAt'), json_extract(body,'$.sampleAt'), json_extract(body,'$.hrBpm')
            FROM captures WHERE json_extract(body,'$.source') IN ('live_hr','history')
            AND coalesce(json_extract(body,'$.sampleAt'),json_extract(body,'$.receivedAt')) >= ?
            AND coalesce(json_extract(body,'$.sampleAt'),json_extract(body,'$.receivedAt')) <= ?
            AND json_extract(body,'$.quality') IN ('live_receipt_timestamp','historical_intervals_unverified')
            AND json_extract(body,'$.hrBpm') BETWEEN 1 AND 255
            ORDER BY coalesce(json_extract(body,'$.sampleAt'),json_extract(body,'$.receivedAt')) DESC, rowid DESC LIMIT ?
            """
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else { throw failure() }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_double(statement, 1, since.timeIntervalSince1970)
        sqlite3_bind_double(statement, 2, through.timeIntervalSince1970)
        sqlite3_bind_int(statement, 3, Int32(max(0, min(limit, 7200)) * 3))
        var rows: [HeartRateReading] = []
        var status = sqlite3_step(statement)
        while status == SQLITE_ROW {
            guard let id = sqlite3_column_text(statement, 0), let source = sqlite3_column_text(statement, 1),
                  let quality = sqlite3_column_text(statement, 2), sqlite3_column_type(statement, 3) != SQLITE_NULL else { throw failure() }
            let sampleAt: Double? = sqlite3_column_type(statement, 4) == SQLITE_NULL ? nil : sqlite3_column_double(statement, 4)
            if let reading = HeartRateReading(id: String(cString: id), source: String(cString: source),
                                             quality: String(cString: quality), receivedAt: sqlite3_column_double(statement, 3),
                                             sampleAt: sampleAt, bpm: Int(sqlite3_column_int(statement, 5))) { rows.append(reading) }
            status = sqlite3_step(statement)
        }
        guard status == SQLITE_DONE else { throw failure() }
        return Array(HeartRateTimeline.mergingHistory(rows).suffix(max(0, min(limit, 7200))))
    }

    public func exportStatus() throws -> (pending: Int, lastAcknowledged: Date?) {
        var statement: OpaquePointer?
        let sql = "SELECT (SELECT value FROM metadata WHERE key='pendingCount'), (SELECT value FROM metadata WHERE key='lastAcknowledged')"
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else { throw failure() }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_step(statement) == SQLITE_ROW else { throw failure() }
        let date = sqlite3_column_type(statement, 1) == SQLITE_NULL ? nil : Date(timeIntervalSince1970: sqlite3_column_double(statement, 1))
        return (Int(sqlite3_column_int64(statement, 0)), date)
    }
    public func acknowledge(_ ids: [String], at date: Date = Date()) throws {
        guard !ids.isEmpty else { return }
        try execute("BEGIN IMMEDIATE")
        do {
            var statement: OpaquePointer?
            guard sqlite3_prepare_v2(db, "UPDATE captures SET uploaded=1 WHERE id=?", -1, &statement, nil) == SQLITE_OK else { throw failure() }
            defer { sqlite3_finalize(statement) }
            for id in ids {
                sqlite3_reset(statement); sqlite3_clear_bindings(statement)
                sqlite3_bind_text(statement, 1, id, -1, transient)
                guard sqlite3_step(statement) == SQLITE_DONE else { throw failure() }
            }
            try execute("INSERT INTO metadata(key,value) VALUES('lastAcknowledged',\(date.timeIntervalSince1970)) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)")
            try execute("COMMIT")
        } catch { try? execute("ROLLBACK"); throw error }
    }
}

/// Persist this state per strap; alerts re-arm only after a meaningful battery rise.
public struct BatteryAlerts: Codable {
    public var fired: Set<Int> = []
    public init() {}
    public mutating func observe(_ percent: Int) -> Int? {
        guard (0...100).contains(percent) else { return nil }
        for threshold in [20, 10] where percent >= threshold + 5 { fired.remove(threshold) }
        let crossed = [10, 20].filter { percent <= $0 && !fired.contains($0) }
        guard let alert = crossed.first else { return nil }
        // A first observation at 8% produces one urgent alert, not two notifications.
        for threshold in [20, 10] where percent <= threshold { fired.insert(threshold) }
        return alert
    }
}
