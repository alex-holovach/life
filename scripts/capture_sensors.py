#!/usr/bin/env python3
"""Bounded WHOOP 4 sensor experiment through an already paired Mac/Linux adapter.

Sends only status reads, one unacknowledged history request, and reversible live
stream toggles. Never changes RTC, flash cursors, firmware, calibration or persistent
optical modes. Every command and notification is written before interpretation.
"""
import argparse
import asyncio
import base64
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
import uuid

from bleak import BleakClient, BleakScanner
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from whoop_protocol import crc8, Frames, decode
import zlib

SUFFIX = "-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD = "61080002" + SUFFIX
HR = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
ALLOWED = {3, 7, 11, 20, 22, 26, 34, 35, 40, 42, 44, 62, 63, 81, 82, 98, 106, 107}
CLEANUP = [(20, [0]), (63, [0]), (106, [0]), (107, [1, 0]), (82, [1]), (3, [1])]


def command(code, seq, payload):
    if code not in ALLOWED:
        raise ValueError("Command excluded from sensor research")
    body = bytes([35, seq, code, *payload])
    size = (len(body) + 4).to_bytes(2, "little")
    return bytes([170]) + size + bytes([crc8(size)]) + body + zlib.crc32(body).to_bytes(4, "little")


async def run(args):
    os.umask(0o077)
    targets = json.loads(args.discovery.read_text())
    if len(targets) != 1:
        raise SystemExit("Expected exactly one previously selected strap")
    target = targets[0]
    print("Scanning for the paired strap (20-second deadline)", flush=True)
    device = await asyncio.wait_for(BleakScanner.find_device_by_address(target["address"], timeout=12), 20)
    if device is None:
        raise SystemExit("Paired strap is not advertising nearby; no commands sent")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    session = str(uuid.uuid4())
    counts = Counter(); parsers = {}; seq = 0; cleanup_errors = []
    with args.output.open("x") as output:
        def save(source, raw, **fields):
            row = dict(id=str(uuid.uuid4()), deviceId=target["address"], sessionId=session,
                       receivedAt=time.time(), source=source, rawBase64=base64.b64encode(raw).decode(),
                       decoderVersion="whoop4-research-v1", **fields)
            output.write(json.dumps(row) + "\n"); output.flush()

        def notify(characteristic, raw):
            raw = bytes(raw); channel = characteristic.uuid.lower()
            if channel == HR:
                flags = raw[0] if raw else 0
                off = 3 if flags & 1 else 2
                bpm = int.from_bytes(raw[1:off], "little")
                if flags & 8: off += 2
                words = [int.from_bytes(raw[i:i+2], "little") for i in range(off,len(raw)-1,2)] if flags & 16 else []
                save("live_hr", raw, hrBpm=bpm, intervalWords=words, intervalStatus="units_and_timing_unverified")
                counts["standard_hr"] += 1
                return
            save(channel, raw)
            if channel[:8] in ("61080003", "61080004", "61080005"):
                parser = parsers.setdefault(channel, Frames())
                for packet in parser.append(raw):
                    d = decode(packet)
                    if d is not None:
                        counts[f'type_{d["packet_type"]:02x}/v{d["version"]}/len{d["length"]}'] += 1

        async with BleakClient(device, timeout=20) as client:
            async def send(code, payload=[0]):
                nonlocal seq
                seq = (seq + 1) & 255
                packet = command(code, seq, payload)
                save("research_command", packet)
                await asyncio.wait_for(client.write_gatt_char(CMD, packet, response=True), 5)

            try:
                print("Connected; inventorying services", flush=True)
                for service in client.services:
                    for ch in service.characteristics:
                        save("gatt_inventory", f"service={service.uuid};characteristic={ch.uuid};properties={','.join(ch.properties)}".encode())
                await send(26)
                for ch in [HR, BATTERY] + ["6108000"+str(n)+SUFFIX for n in (3,4,5,7)]:
                    characteristic = client.services.get_characteristic(ch)
                    if characteristic and ("notify" in characteristic.properties or "indicate" in characteristic.properties):
                        await asyncio.wait_for(client.start_notify(characteristic, notify), 5)
                for code in (35, 7): await send(code)
                await send(3,[1])
                for code in (11,34,98,40,42,44,62): await send(code)
                await asyncio.sleep(3)
                print("Reading one history batch, without ACK", flush=True)
                await send(22)
                await asyncio.sleep(12)
                await send(20)
                print("Capturing motion for 15 seconds", flush=True)
                for code in (63,81,106): await send(code,[1])
                await asyncio.sleep(15)
                for code,payload in [(63,[0]),(106,[0]),(82,[1])]: await send(code,payload)
                print("Capturing wrist-gated optical data for 15 seconds", flush=True)
                await send(63,[1]); await send(107,[1,1])
                await asyncio.sleep(15)
            finally:
                print("Disabling extra streams", flush=True)
                for code,payload in CLEANUP:
                    try: await send(code,payload)
                    except Exception as error: cleanup_errors.append(f"command {code}: {type(error).__name__}")
                before = counts.copy()
                if client.is_connected: await asyncio.sleep(8)
                output.flush(); os.fsync(output.fileno())
                print(json.dumps(dict(frames=dict(counts), after_cleanup=dict(counts-before), cleanup_errors=cleanup_errors)), flush=True)
    if cleanup_errors:
        raise SystemExit("Cleanup incomplete: in Life Settings, use Stop extra sensor streams")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
