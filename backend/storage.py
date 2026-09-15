"""Crash-safe compressed archive and SQLite outbox. One writer worker per database."""
import gzip
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from integrations.whoop import samples
from whoop_protocol import capture_summary
import temperature
import hrv
import vitals
import wellness

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS firmware_observations(
 device TEXT NOT NULL, observed_at REAL NOT NULL, firmware TEXT NOT NULL,
 capture_id TEXT NOT NULL, PRIMARY KEY(device,observed_at));
CREATE TABLE IF NOT EXISTS captures(
 device TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, archive TEXT NOT NULL,
 ordinal INTEGER NOT NULL, received REAL NOT NULL, source TEXT NOT NULL,
 battery REAL, battery_at REAL, PRIMARY KEY(device,id));
CREATE TABLE IF NOT EXISTS samples(
 series TEXT NOT NULL, timestamp INTEGER NOT NULL, value REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, error TEXT, PRIMARY KEY(series,timestamp));
CREATE INDEX IF NOT EXISTS samples_pending ON samples(state,next_attempt,timestamp);
CREATE TABLE IF NOT EXISTS conflicts(
 series TEXT NOT NULL, timestamp INTEGER NOT NULL, proposed REAL NOT NULL,
 archive TEXT NOT NULL, PRIMARY KEY(series,timestamp,proposed));
CREATE TABLE IF NOT EXISTS sensor_inventory(
 kind TEXT PRIMARY KEY, received REAL NOT NULL, capture_id TEXT NOT NULL,
 frames INTEGER NOT NULL, summary TEXT NOT NULL);
"""

class Conflict(Exception): pass


def directory():
    path = Path(os.environ.get("DATA_DIR", "data"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@contextmanager
def connect():
    db = sqlite3.connect(directory() / "ingest.sqlite", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA + hrv.SCHEMA + vitals.SCHEMA + wellness.SCHEMA)


def persist(batch):
    # The canonical batch preserves every validated field, including raw bytes and ordered words.
    data = batch.model_dump_json(exclude_none=True).encode()
    digest = hashlib.sha256(data).hexdigest()
    folder = directory() / "archive" / digest[:2]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / (digest + ".json.gz")
    # Atomic publish followed by directory fsync precedes database references and the HTTP ACK.
    if not target.exists():
        import tempfile
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".batch-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(gzip.compress(data, mtime=0)); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, target)
            for path in (folder, folder.parent, directory()):
                descriptor = os.open(path, os.O_RDONLY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    archive = str(target.relative_to(directory()))
    now = time.time()
    cutoff = int((now - int(os.environ.get("BACKLOG_DAYS", "30")) * 86400) * 1000)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for ordinal, c in enumerate(batch.captures):
            # One device alias keeps metric cardinality bounded and prevents accidental strap mixing.
            bound = db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
            if bound is not None and bound[0] != c.deviceId:
                raise Conflict("A different strap is already bound to this collector")
            db.execute("INSERT OR IGNORE INTO settings VALUES('device',?)", (c.deviceId,))
            identity = c.model_dump(exclude_none=True)
            if c.source == "history":
                identity.pop("receivedAt", None); identity.pop("sessionId", None)
            fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = db.execute("SELECT digest FROM captures WHERE device=? AND id=?", (c.deviceId, c.id)).fetchone()
            if previous:
                if previous[0] != fingerprint: raise Conflict("Capture ID reused with different content")
                continue
            verified = c.source == "battery_verified" and c.quality == "cross_checked" and c.batteryPercent is not None
            db.execute("INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,?)", (c.deviceId, c.id, fingerprint, archive, ordinal, c.receivedAt, c.source,
                c.batteryPercent if verified else None, (c.sampleAt or c.receivedAt) if verified else None))
            decoded = capture_summary(c)
            if decoded is not None:
                # Fixed decoder layout names keep inventory cardinality bounded. Delayed
                # uploads increment counts without replacing the latest observation.
                db.execute("""INSERT INTO sensor_inventory VALUES(?,?,?,1,?)
                    ON CONFLICT(kind) DO UPDATE SET frames=frames+1,
                    summary=CASE WHEN excluded.received >= received THEN excluded.summary ELSE summary END,
                    capture_id=CASE WHEN excluded.received >= received THEN excluded.capture_id ELSE capture_id END,
                    received=max(received,excluded.received)""",
                    (decoded["layout"], c.receivedAt, c.id, json.dumps(decoded, allow_nan=False)))
            temperature.record_firmware(c, db)
            for series, at, value in samples(c) + temperature.samples(c, db):
                old = db.execute("SELECT value FROM samples WHERE series=? AND timestamp=?", (series, at)).fetchone()
                if old:
                    if old[0] != value:
                        db.execute("INSERT OR IGNORE INTO conflicts VALUES(?,?,?,?)", (series, at, value, archive))
                    continue
                state = "historical_import" if at < cutoff else "pending"
                db.execute("INSERT INTO samples(series,timestamp,value,state) VALUES(?,?,?,?)", (series, at, value, state))


def sensor_inventory():
    with connect() as db:
        return [dict(kind=r["kind"], received_at=r["received"], frames=r["frames"],
                     decoded=json.loads(r["summary"])) for r in db.execute("SELECT * FROM sensor_inventory ORDER BY kind")]


def status():
    with connect() as db:
        counts = {r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM samples GROUP BY state")}
        # Local retry/lifecycle logs can continue while the strap is unreachable.
        # They must not reset the stale-strap alert or dashboard traffic age.
        last = db.execute("SELECT max(received) FROM captures WHERE source NOT IN ('collector_event','research_command')").fetchone()[0]
        battery = db.execute("SELECT battery,battery_at FROM captures WHERE battery IS NOT NULL ORDER BY battery_at DESC LIMIT 1").fetchone()
        last_success = db.execute("SELECT value FROM settings WHERE key='last_prometheus_success'").fetchone()
        return dict(captured_records=db.execute("SELECT count(*) FROM captures").fetchone()[0],
                    samples=counts, conflicts=db.execute("SELECT count(*) FROM conflicts").fetchone()[0],
                    last_received=last, battery_percent=battery[0] if battery else None,
                    battery_observed_at=battery[1] if battery else None,
                    last_prometheus_success=float(last_success[0]) if last_success else None)
