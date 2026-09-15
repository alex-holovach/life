"""Read battery and subscribe to standard HR on the already discovered WHOOP.

No custom commands, history acknowledgements, clock changes, or firmware writes.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import time

from bleak import BleakClient, BleakScanner

ROOT = Path(__file__).resolve().parents[1]
HR = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"


def decode_hr(data):
    if len(data) < 2:
        raise ValueError("Truncated HR packet")
    flags = data[0]
    offset = 3 if flags & 1 else 2
    if len(data) < offset:
        raise ValueError("Truncated HR field")
    bpm = struct.unpack_from("<H", data, 1)[0] if flags & 1 else data[1]
    if flags & 8:
        offset += 2
    if len(data) < offset:
        raise ValueError("Truncated energy field")
    rr = []
    if flags & 16:
        if (len(data) - offset) % 2:
            raise ValueError("Truncated RR field")
        rr = [struct.unpack_from("<H", data, i)[0] * 1000 / 1024
              for i in range(offset, len(data), 2)]
    return {"hr_bpm": bpm, "rr_ms": rr,
            "contact_detected": bool(flags & 2) if flags & 4 else None}


async def main():
    os.umask(0o077)
    candidates = json.loads((ROOT / "data/discovery.json").read_text())
    if len(candidates) != 1:
        raise SystemExit("Expected one previously identified WHOOP; rescan and select the target first.")
    target = candidates[0]
    print(f"Looking for {target['name']}…", flush=True)
    device = await BleakScanner.find_device_by_address(target["address"], timeout=20)
    if device is None:
        raise SystemExit("The identified WHOOP is no longer advertising nearby.")
    report = {"device": target, "started_at": datetime.now(timezone.utc).isoformat(),
              "battery_percent": None, "samples": [], "errors": [], "services": []}
    output = ROOT / "data" / ("probe-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")

    def notification(characteristic, data):
        sample = {"received_at": time.time(), "raw_hex": bytes(data).hex()}
        try:
            sample.update(decode_hr(data))
        except ValueError as error:
            sample["error"] = str(error)
        report["samples"].append(sample)
        if len(report["samples"]) <= 3:
            print("Live HR:", json.dumps(sample), flush=True)

    try:
        async with BleakClient(device, timeout=25) as client:
            print("Connected.", flush=True)
            for service in client.services:
                report["services"].append({"uuid": service.uuid, "characteristics": [
                    {"uuid": c.uuid, "properties": c.properties} for c in service.characteristics]})
            hr = client.services.get_characteristic(HR)
            battery = client.services.get_characteristic(BATTERY)
            print(json.dumps({"heart_rate_characteristic": hr is not None,
                              "battery_characteristic": battery is not None}), flush=True)
            if hr is not None:
                try:
                    await asyncio.wait_for(client.start_notify(hr, notification), timeout=15)
                except Exception as error:
                    report["errors"].append("HR subscription: " + str(error))
                    print(report["errors"][-1], flush=True)
            if battery is not None:
                try:
                    raw = bytes(await asyncio.wait_for(client.read_gatt_char(battery), timeout=10))
                    report["battery_raw_hex"] = raw.hex()
                    if len(raw) == 1 and raw[0] <= 100:
                        report["battery_percent"] = raw[0]
                    print("Battery:", json.dumps({"percent": report["battery_percent"], "raw": raw.hex()}), flush=True)
                except Exception as error:
                    report["errors"].append("Battery read: " + str(error))
                    print(report["errors"][-1], flush=True)
            else:
                report["errors"].append("Standard Battery Level characteristic absent; custom battery query required.")
            print("Listening for 30 seconds…", flush=True)
            await asyncio.sleep(30)
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n")
        rates = [s["hr_bpm"] for s in report["samples"] if "hr_bpm" in s]
        intervals = [rr for s in report["samples"] for rr in s.get("rr_ms", [])]
        print(json.dumps({"saved": str(output), "battery_percent": report["battery_percent"],
                          "hr_packets": len(rates), "hr_min": min(rates) if rates else None,
                          "hr_max": max(rates) if rates else None, "rr_intervals": len(intervals),
                          "errors": report["errors"]}, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
