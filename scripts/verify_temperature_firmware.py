"""Execute the temperature conversion and R24 packing instructions offline.

Usage: uv run --with unicorn python scripts/verify_temperature_firmware.py /path/to/harvard-41.17.4.decompressed.bin
The firmware is not distributed with Life. This never connects to or writes a device.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
from unicorn.arm_const import (UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC, UC_ARM_REG_SP,
                              UC_ARM_REG_R0, UC_ARM_REG_R4, UC_ARM_REG_LR, UC_ARM_REG_S0)

HASH = '2c4d7a9edf90e4b39bcecda28d2cd9949bd19de533a14d31a7409b31144c309b'
BASE = 0x10028000


def verify(binary):
    if hashlib.sha256(binary).hexdigest() != HASH:
        raise ValueError('Firmware hash does not match the investigated 41.17.4.0 binary')
    cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    cpu.mem_map(0x10000000, 0x200000); cpu.mem_write(BASE, binary)
    cpu.mem_map(0x20000000, 0x20000)
    cpu.reg_write(UC_ARM_REG_C1_C0_2, 0x00f00000)
    cpu.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
    cpu.reg_write(UC_ARM_REG_SP, 0x2001f000)
    rows = []
    for counts, expected in [(-200, -10), (0, 0), (2000, 100), (6000, 300),
                             (6730, 336), (6740, 337), (6750, 338), (6800, 340),
                             (10000, 500), (14000, 700)]:
        cpu.reg_write(UC_ARM_REG_R0, counts & 0xffff)
        cpu.reg_write(UC_ARM_REG_LR, BASE + 0x381)
        cpu.emu_start((BASE + 0x2e898) | 1, BASE + 0x380, count=100)
        celsius = struct.unpack('<f', struct.pack('<I', cpu.reg_read(UC_ARM_REG_S0)))[0]
        obj = 0x20001000
        cpu.mem_write(obj + 0x31bc, struct.pack('<f', celsius))
        cpu.reg_write(UC_ARM_REG_R4, obj)
        cpu.emu_start((BASE + 0x34786) | 1, BASE + 0x347a8, count=200)
        word = struct.unpack('<h', cpu.mem_read(obj + 0x300c, 2))[0]
        assert word == expected, (counts, word, expected)
        assert abs(word / 10 - celsius) <= .051
        rows.append(dict(sensor_counts=counts, driver_celsius=celsius,
                         packet_word=word, decoded_celsius=word/10))
    return dict(firmware_sha256=HASH, cases=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('firmware', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.firmware.read_bytes()), indent=2))
