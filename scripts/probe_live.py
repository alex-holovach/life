"""Bounded live-stream test. No historical ACK, RTC change, reset, or firmware operation."""
import asyncio
import binascii
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import time

from bleak import BleakClient, BleakScanner
from probe import decode_hr, HR, BATTERY

ROOT = Path(__file__).resolve().parents[1]
CMD = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CHANNELS = [f"6108000{i}-8d6d-82b8-614a-1c8cb0f8dcc6" for i in [3, 4, 5, 7]]

def crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 7) & 255 if crc & 128 else (crc << 1) & 255
    return crc

def command(number, seq, payload=b"\x00"):
    body = bytes([35, seq, number]) + payload
    size = struct.pack("<H", len(body)+4)
    return b"\xaa" + size + bytes([crc8(size)]) + body + struct.pack("<I", binascii.crc32(body))

async def main():
    os.umask(0o077)
    target, = json.loads((ROOT / "data/discovery.json").read_text())
    print(f"Reconnecting to {target['name']}…", flush=True)
    device = await BleakScanner.find_device_by_address(target["address"], timeout=20)
    if device is None:
        raise SystemExit("WHOOP not advertising nearby")
    report = {"device": target, "battery_percent": None, "notifications": [], "frames": [], "errors": []}
    buffers = {}; counts = Counter(); crc_errors = 0
    output = ROOT / "data" / ("live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")

    def notify(characteristic, raw):
        nonlocal crc_errors
        data = bytes(raw); channel = characteristic.uuid
        item = {"received_at": time.time(), "channel": channel, "raw_hex": data.hex()}
        report["notifications"].append(item)
        if channel == HR:
            item.update(decode_hr(data)); counts["standard_hr"] += 1
            if counts["standard_hr"] <= 3:
                print("Standard HR:", item["hr_bpm"], "RR:", item["rr_ms"], flush=True)
            return
        buf = buffers.setdefault(channel, bytearray()); buf.extend(data)
        while len(buf) >= 4:
            if buf[0] != 0xaa or crc8(buf[1:3]) != buf[3]:
                del buf[0]; continue
            length = struct.unpack_from("<H", buf, 1)[0] + 4
            if length < 11 or length > 16384:
                del buf[0]; continue
            if len(buf) < length:
                break
            frame = bytes(buf[:length]); del buf[:length]
            valid = binascii.crc32(frame[4:-4]) == struct.unpack_from("<I", frame, len(frame)-4)[0]
            entry = {"received_at": item["received_at"], "type": frame[4], "code": frame[6],
                     "crc_valid": valid, "raw_hex": frame.hex()}
            report["frames"].append(entry); counts[f"type_{frame[4]}"] += 1
            if not valid:
                crc_errors += 1; continue
            if frame[4] == 40 and len(frame) >= 18:
                count = frame[13]
                if count <= 8 and 14+count*2 <= len(frame)-4:
                    entry["hr_bpm"] = frame[12]
                    entry["rr_raw"] = [struct.unpack_from("<H", frame, 14+2*i)[0] for i in range(count)]
                    if counts["type_40"] <= 3:
                        print("Custom live HR:", entry["hr_bpm"], "raw intervals:", entry["rr_raw"], flush=True)
            elif frame[4] == 36:
                print("Command response:", frame[6], "payload:", frame[7:-4].hex()[:80], flush=True)
            elif frame[4] == 48 and frame[6] in (9,10):
                print("Wrist event:", "on wrist" if frame[6] == 9 else "off wrist", flush=True)

    try:
        async with BleakClient(device, timeout=25) as client:
            print("Connected.", flush=True)
            bat = client.services.get_characteristic(BATTERY)
            if bat:
                raw = bytes(await asyncio.wait_for(client.read_gatt_char(bat), 10))
                if len(raw) == 1 and raw[0] <= 100: report["battery_percent"] = raw[0]
                print("Battery:", report["battery_percent"], flush=True)
            # Confirmed benign battery query establishes the encrypted custom link when needed.
            await asyncio.wait_for(client.write_gatt_char(CMD, command(26, 1), response=True), 15)
            for uuid in [HR] + CHANNELS:
                characteristic = client.services.get_characteristic(uuid)
                if characteristic:
                    try:
                        await asyncio.wait_for(client.start_notify(characteristic, notify), 10)
                    except Exception as error:
                        report["errors"].append(f"Subscribe {uuid}: {error}")
            await client.write_gatt_char(CMD, command(35, 2), response=True)
            await client.write_gatt_char(CMD, command(7, 3), response=True)
            await client.write_gatt_char(CMD, command(3, 4, b"\x01"), response=True)
            print("Live stream requested. Listening for 45 seconds…", flush=True)
            try:
                await asyncio.sleep(45)
            finally:
                if client.is_connected:
                    await client.write_gatt_char(CMD, command(3, 5, b"\x00"), response=True)
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n")
        hr = [f["hr_bpm"] for f in report["frames"] if f.get("hr_bpm", 0) > 0]
        rr = [v for f in report["frames"] for v in f.get("rr_raw", []) if v > 0]
        print(json.dumps({"saved": str(output), "battery_percent": report["battery_percent"],
                          "counts": counts, "crc_errors": crc_errors,
                          "valid_hr_packets": len(hr), "positive_raw_intervals": len(rr),
                          "custom_hr_min": min(hr) if hr else None, "custom_hr_max": max(hr) if hr else None,
                          "errors": report["errors"]}, indent=2), flush=True)

if __name__ == "__main__":
    asyncio.run(main())
