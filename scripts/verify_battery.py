"""Cross-check the standard gauge against the WHOOP read-only battery command."""
import asyncio
import binascii
from datetime import datetime, timezone
import json
import os
import struct
import time

from bleak import BleakClient, BleakScanner
from probe_live import ROOT, CMD, CHANNELS, command, crc8
from probe import BATTERY

async def main():
    os.umask(0o077)
    target, = json.loads((ROOT / "data/discovery.json").read_text())
    device = await BleakScanner.find_device_by_address(target["address"], timeout=20)
    if device is None: raise SystemExit("WHOOP not advertising")
    report = {"standard_readings": [], "custom_responses": []}
    buffers = {}
    def response(characteristic, value):
        buf = buffers.setdefault(characteristic.uuid, bytearray()); buf.extend(value)
        while len(buf) >= 4:
            if buf[0] != 0xaa or crc8(buf[1:3]) != buf[3]: del buf[0]; continue
            size = struct.unpack_from("<H",buf,1)[0]+4
            if size < 11 or size > 16384: del buf[0]; continue
            if len(buf) < size: break
            frame = bytes(buf[:size]); del buf[:size]
            if binascii.crc32(frame[4:-4]) != struct.unpack_from("<I",frame,size-4)[0]: continue
            if frame[4] == 36 and frame[6] == 26:
                payload = frame[7:-4]
                item = {"raw":frame.hex(),"received_at":time.time()}
                if len(payload)>=4:
                    item["status"] = payload[1]
                    if payload[1] == 1:
                        item["battery_percent"] = struct.unpack_from("<H",payload,2)[0]/10
                report["custom_responses"].append(item)
                print("WHOOP battery response:",json.dumps(item),flush=True)
    async with BleakClient(device,timeout=25) as client:
        for uuid in CHANNELS:
            c=client.services.get_characteristic(uuid)
            if c: await asyncio.wait_for(client.start_notify(c,response),10)
        for seq in range(1,4):
            raw=bytes(await asyncio.wait_for(client.read_gatt_char(BATTERY),10))
            item={"received_at":time.time(),"raw":raw.hex(),"percent":raw[0] if len(raw)==1 else None}
            report["standard_readings"].append(item)
            print("Standard battery:",json.dumps(item),flush=True)
            await asyncio.wait_for(client.write_gatt_char(CMD,command(26,seq),response=True),10)
            await asyncio.sleep(3)
    output=ROOT/'data'/('battery-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    output.write_text(json.dumps(report,indent=2)+'\n')
    print("Saved:",output,flush=True)

if __name__=='__main__':asyncio.run(main())
