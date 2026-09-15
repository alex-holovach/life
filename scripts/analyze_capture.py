#!/usr/bin/env python3
"""Offline sensor inventory. Reads local iPhone SQLite, JSONL or backend archive directories.

Never connects to the strap, writes source data, or publishes experimental health metrics.
Usage: python3 scripts/analyze_capture.py CAPTURE_PATH --output report.json
"""
import argparse
import base64
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from whoop_protocol import Frames, decode


def captures(path):
    if path.is_dir():
        for file in sorted(path.rglob("*.json.gz")):
            yield from json.loads(gzip.decompress(file.read_bytes()))["captures"]
    elif path.suffix == ".sqlite":
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for row in db.execute("SELECT body FROM captures"):
                yield json.loads(row[0])
    else:
        with path.open() as source:
            for line in source:
                if line.strip():
                    yield json.loads(line)


def summarize(rows):
    records = {row["id"]: row for row in rows}
    rows = sorted(records.values(), key=lambda c: c["receivedAt"])
    streams = defaultdict(Frames)
    layouts, types, events, replies, sources = Counter(), Counter(), Counter(), Counter(), Counter()
    fields = defaultdict(lambda: defaultdict(list))
    waveforms = defaultdict(Counter)
    clocks = defaultdict(list)
    bad_crc = 0
    hr = []
    command_prefixes = {}
    research_commands = []
    inventory = set()
    raw_sessions = {c["sessionId"] for c in rows if c["source"].lower().startswith("6108000")}
    for c in rows:
        sources[c["source"]] += 1
        raw = base64.b64decode(c["rawBase64"])
        source = c["source"].lower()
        if source == "live_hr":
            hr.append(c)
        if source == "research_command":
            research_commands.append(dict(received=c["receivedAt"], command=raw[6], payload_hex=raw[7:-4].hex()))
        if source == "gatt_inventory":
            inventory.add(raw.decode("utf-8", errors="replace"))
        chunks = []
        if source[:8] in ("61080003", "61080004", "61080005"):
            chunks = streams[(c["deviceId"], c["sessionId"], source)].append(raw)
        elif source == "wire_frame" and c["sessionId"] not in raw_sessions:
            chunks = [raw]
        for chunk in chunks:
            d = decode(chunk)
            if d is None:
                bad_crc += 1
                continue
            layout = d["layout"]
            layouts[layout] += 1
            types[f'{d["packet_type"]:02x}/v{d["version"]}/len{d["length"]}'] += 1
            if "event_code" in d:
                events[d["event_code"]] += 1
            if "command_code" in d:
                cmd = d["command_code"]
                replies[cmd] += 1
                command_prefixes[cmd] = chunk[7:min(len(chunk)-4, 23)].hex()
            if "device_time_raw" in d:
                clocks[d["time_status"]].append(d["device_time_raw"])
            for key, value in d["fields"].items():
                fields[layout][key].append(value)
            for key, samples in d["waveforms"].items():
                waveforms[layout][key] += len(samples)
    def stats(values):
        return dict(count=len(values), min=min(values), max=max(values), unique=len(set(values)))
    intervals = [c for c in hr if c.get("intervalWords") and c.get("hrBpm", 0) > 0]
    scale_errors = {}
    for label, scale in (("milliseconds", 1), ("bluetooth_1024_ticks", 1000/1024)):
        errors = [abs(60000/(statistics.mean(c["intervalWords"])*scale)-c["hrBpm"])
                  for c in intervals if min(c["intervalWords"]) > 0]
        if errors:
            scale_errors[label] = dict(median_bpm_error=statistics.median(errors), mean_bpm_error=statistics.mean(errors))
    connected = expected = 0
    for a, b in zip(hr, hr[1:]):
        dt = b["receivedAt"] - a["receivedAt"]
        if a["sessionId"] == b["sessionId"] and 0 < dt <= 5 and a.get("hrBpm", 0) > 0:
            connected += dt
            expected += a["hrBpm"] * dt / 60
    count = sum(len(c["intervalWords"]) for c in intervals)
    return dict(records=len(rows), sources=dict(sources), layouts=dict(layouts), frame_types=dict(types),
                bad_crc=bad_crc, discarded_bytes=sum(p.discarded for p in streams.values()),
                incomplete_streams=sum(bool(p.buffer) for p in streams.values()),
                events=dict(events), responses=dict(replies), response_prefixes=command_prefixes,
                research_commands=research_commands, gatt_inventory=sorted(inventory),
                clocks={k: stats(v) for k,v in clocks.items()},
                scalar_ranges={layout:{k:stats(v) for k,v in fs.items()} for layout,fs in fields.items()},
                waveform_sample_counts={k:dict(v) for k,v in waveforms.items()},
                intervals=dict(hr_notifications=len(hr), notifications_with_intervals=len(intervals),
                               interval_count=count, connected_seconds=connected,
                               approximate_beats_from_hr=expected,
                               reported_to_expected_ratio=count/expected if expected else None,
                               unit_hypothesis_errors=scale_errors,
                               warning="HR comparison cannot validate interval units or continuity; no HRV computed"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = summarize(captures(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(mode=0o600, exist_ok=True)
    args.output.chmod(0o600)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key:report[key] for key in ("records", "layouts", "bad_crc", "clocks", "intervals")}, indent=2))


if __name__ == "__main__":
    main()
