"""Online snapshots of Life's stores. Never stops or restarts a service."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
import urllib.request


def snapshot_sqlite(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 60
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup exceeded 60 seconds")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup SQLite integrity check failed")


def create_backup(root, values, compose):
    root = root.resolve()
    folder = root / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    # Serialize backups, without locking the app or its databases during compression.
    with (folder / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        unsupported = [p.name for p in (root / "data").iterdir() if p.name not in {"ingest", "grafana", "prometheus"}]
        if unsupported:
            raise RuntimeError("Add snapshot support for integration stores: " + ", ".join(unsupported))
        snapshot = None
        filename = folder / ("life-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".tar.gz")
        if filename.exists(): raise FileExistsError(filename)
        temporary = filename.with_suffix(".partial")
        try:
            with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=folder) as staging:
                stage = Path(staging)
                # Capture the outbox BEFORE Prometheus. Every sample marked sent here
                # already exists in the later TSDB snapshot. Pending samples may replay
                # harmlessly after restore, using identical series/timestamps/values.
                ingest = stage / "data/ingest/ingest.sqlite"
                snapshot_sqlite(root / "data/ingest/ingest.sqlite", ingest)
                with sqlite3.connect(ingest) as db:
                    references = db.execute("SELECT archive FROM captures UNION SELECT archive FROM conflicts").fetchall()
                for (name,) in references:
                    relative = Path(name)
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "archive":
                        raise ValueError("Invalid archive reference")
                    target = stage / "data/ingest" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Published archives are immutable. Hard links keep staging cheap.
                    os.link(root / "data/ingest" / relative, target)
                grafana = stage / "data/grafana"
                shutil.copytree(root / "data/grafana", grafana,
                                ignore=shutil.ignore_patterns("grafana.db", "grafana.db-wal", "grafana.db-shm", "png", "csv", "pdf"))
                snapshot_sqlite(root / "data/grafana/grafana.db", grafana / "grafana.db")
                url = "http://127.0.0.1:" + str(int(values.get("PROMETHEUS_PORT", "9090")))
                req = urllib.request.Request(url + "/api/v1/admin/tsdb/snapshot?skip_head=false", method="POST")
                with urllib.request.urlopen(req, timeout=120) as response:
                    result = json.load(response)
                name = result.get("data", {}).get("name", "")
                if result.get("status") != "success" or not re.fullmatch(r"[A-Za-z0-9-]+", name):
                    raise RuntimeError("Prometheus did not return a valid snapshot")
                snapshot = root / "data/prometheus/snapshots" / name
                # Prometheus 3.14 Snapshot uses the in-order head and omits recent
                # out-of-order head samples. Supplement the snapshot with blocks from
                # the durable numeric outbox. Identical overlapping samples merge on
                # read/compaction; never change their timestamps or values.
                metrics = stage / "telemetry.openmetrics"
                cutoff = int((time.time() - int(values.get("RETENTION_DAYS", "365")) * 86400) * 1000)
                count = 0
                with sqlite3.connect(ingest) as db, metrics.open("w") as output:
                    for series, timestamp, value in db.execute("SELECT series,timestamp,value FROM samples WHERE state='sent' AND timestamp >= ? ORDER BY series,timestamp", (cutoff,)):
                        labels = json.loads(series)
                        metric = labels.pop("__name__")
                        label_text = ",".join(key + "=" + json.dumps(val, ensure_ascii=False) for key, val in sorted(labels.items()))
                        output.write(f"{metric}{{{label_text}}} {value!r} {timestamp // 1000}.{timestamp % 1000:03d}\n")
                        count += 1
                    output.write("# EOF\n")
                blocks = stage / "telemetry-blocks"
                if count:
                    compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "/bin/promtool", "-v", str(stage) + ":/backup", "prometheus", "tsdb", "create-blocks-from", "openmetrics", "/backup/telemetry.openmetrics", "/backup/telemetry-blocks", capture=True)
                metrics.unlink()
                for item in ("secrets", ".env", "compose.yaml", "prometheus", "grafana", "gateway", "integrations"):
                    source = root / item
                    if source.is_dir(): shutil.copytree(source, stage / item)
                    else: shutil.copy2(source, stage / item)
                (stage / "backup.json").write_text(json.dumps({"format": 2, "method": "online", "created_at": time.time(), "prometheus_snapshot": name, "supplemented_numeric_samples": count}) + "\n")
                with tarfile.open(temporary, "w:gz", compresslevel=1, dereference=True) as archive:
                    for child in sorted(stage.iterdir()):
                        if child != blocks: archive.add(child, arcname=child.name)
                    archive.add(snapshot, arcname="data/prometheus")
                    if blocks.exists():
                        for block in sorted(blocks.iterdir()):
                            if block.is_dir() and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", block.name):
                                archive.add(block, arcname="data/prometheus/" + block.name)
                os.chmod(temporary, 0o600)
                with temporary.open("rb") as handle: os.fsync(handle.fileno())
                temporary.rename(filename)
                descriptor = os.open(folder, os.O_RDONLY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
            print("Private online backup created: " + str(filename))
            return filename
        finally:
            if temporary.exists(): temporary.unlink()
            if snapshot is not None and snapshot.exists(): shutil.rmtree(snapshot)
