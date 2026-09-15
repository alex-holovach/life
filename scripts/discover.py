"""Scan only: no connections, pairing, or device commands."""
import asyncio
import json
from pathlib import Path
from bleak import BleakScanner

WHOOP4 = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
WHOOP5 = "fd4b0001-cce1-4033-93ce-002d5875f58a"

async def main():
    print("Scanning Bluetooth advertisements for 15 seconds…", flush=True)
    found = await BleakScanner.discover(timeout=15, return_adv=True)
    candidates = []
    for device, advertisement in found.values():
        name = advertisement.local_name or device.name or ""
        services = [s.lower() for s in advertisement.service_uuids]
        if "whoop" in name.lower() or WHOOP4 in services or WHOOP5 in services:
            candidates.append({"name": name, "address": device.address,
                               "rssi": advertisement.rssi, "services": services})
    output = Path(__file__).resolve().parents[1] / "data" / "discovery.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(candidates, indent=2) + "\n")
    print(json.dumps({"nearby_advertisers": len(found), "whoop_candidates": candidates}, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
