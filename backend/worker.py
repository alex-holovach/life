"""Run one durable remote-write worker. Unknown 4xx responses require operator review."""
import fcntl
import os
import random
import sys
import time
import httpx
import storage
from remote_write import encode

HEADERS = {"Content-Encoding": "snappy", "Content-Type": "application/x-protobuf",
           "User-Agent": "life/0.2", "X-Prometheus-Remote-Write-Version": "0.1.0"}


def drain_once(client, now=None):
    now = time.time() if now is None else now
    with storage.connect() as db:
        cutoff = int((now - int(os.environ.get("BACKLOG_DAYS", "30")) * 86400) * 1000)
        db.execute("UPDATE samples SET state='historical_import',error='Outside configured backlog window' WHERE state='pending' AND timestamp<?", (cutoff,))
        rows = db.execute("SELECT * FROM samples WHERE state='pending' ORDER BY timestamp,series LIMIT 1000").fetchall()
    if not rows: return False
    # Do not skip an earlier backed-off sample and advance its series past it.
    if any(r["next_attempt"] > now for r in rows): return False
    try:
        response = client.post(os.environ.get("PROMETHEUS_WRITE_URL", "http://127.0.0.1:9090/api/v1/write"), content=encode(rows), headers=HEADERS)
        code = response.status_code
    except httpx.TransportError:
        code = 0
    if 200 <= code < 300:
        with storage.connect() as db:
            db.executemany("UPDATE samples SET state='sent',error=NULL WHERE series=? AND timestamp=?", [(r["series"], r["timestamp"]) for r in rows])
            db.execute("INSERT INTO settings VALUES('last_prometheus_success',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    elif code == 0 or code == 429 or code >= 500:
        with storage.connect() as db:
            for r in rows:
                delay = min(3600, 2 ** min(r["attempts"] + 1, 11)) * random.uniform(1, 1.25)
                db.execute("UPDATE samples SET attempts=attempts+1,next_attempt=?,error=? WHERE series=? AND timestamp=?",
                           (now + delay, f"Remote write HTTP {code}" if code else "Prometheus unreachable", r["series"], r["timestamp"]))
    else:
        # A request may be partially accepted. Keep all affected samples archived and mark
        # the outcome for review; never retimestamp or endlessly retry a permanent rejection.
        with storage.connect() as db:
            db.executemany("UPDATE samples SET state='rejected',error=? WHERE series=? AND timestamp=?",
                           [(f"Remote write HTTP {code}; acceptance may be partial", r["series"], r["timestamp"]) for r in rows])
    return True


def main():
    os.umask(0o077); storage.initialize()
    with open(storage.directory() / "worker.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            while True:
                with storage.connect() as db:
                    db.execute("INSERT INTO settings VALUES('worker_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
                if not drain_once(client): time.sleep(2)


if __name__ == "__main__":
    if "--health" in sys.argv:
        with storage.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='worker_heartbeat'").fetchone()
        sys.exit(0 if row and time.time() - float(row[0]) < 120 else 1)
    else: main()
