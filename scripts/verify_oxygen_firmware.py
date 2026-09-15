"""Offline execution of the identified SpO2 tail and R24 serializer.

Requires Unicorn and the exact external Harvard 41.17.4.0 binary. No device I/O.
These tests establish software behavior, not physiological accuracy.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
from unicorn.arm_const import (UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC,
    UC_ARM_REG_SP, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R4,
    UC_ARM_REG_R5, UC_ARM_REG_R7, UC_ARM_REG_R11, UC_ARM_REG_S0, UC_ARM_REG_S16)

HASH = '2c4d7a9edf90e4b39bcecda28d2cd9949bd19de533a14d31a7409b31144c309b'
BASE = 0x10028000


def verify(binary):
    if hashlib.sha256(binary).hexdigest() != HASH:
        raise ValueError('Unsupported firmware binary')
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    u.mem_map(0x10000000, 0x200000); u.mem_write(BASE, binary)
    u.mem_map(0x20000000, 0x80000)
    u.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
    u.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
    bits = lambda f: struct.unpack('<I', struct.pack('<f', f))[0]
    cases = []
    for ratio, dc1, dc2, flags, expected in [
        (.67, 1, 1, 0, 98), (.83, 1, 1, 0, 95), (1.09, 1, 1, 0, 90),
        (2.15, 1, 1, 0, 70), (2.3, 1, 1, 0, 1), (.45, 1, 1, 0, 2),
        (.2, 1, 1, 0, 3), (0, 0, 1, 0, 98), (0, 1, 0, 0, 98),
        (.83, 1, 1, 16, 16), (.83, 1, 1, 128, 128),
    ]:
        u.reg_write(UC_ARM_REG_SP, 0x2007e000)
        u.reg_write(UC_ARM_REG_R5, 0x2005d870)
        u.reg_write(UC_ARM_REG_R11, 0x2005d878)
        u.reg_write(UC_ARM_REG_R7, 0x20010000)
        u.reg_write(UC_ARM_REG_S0, bits(dc2)); u.reg_write(UC_ARM_REG_S16, bits(ratio))
        u.mem_write(0x2005d878, struct.pack('<f', dc1))
        u.mem_write(0x2005d870, bytes([0, 0, flags & 16, flags & 128, 0]))
        u.emu_start((BASE + 0x86c78) | 1, BASE + 0x86c1a, count=10000)
        value = u.reg_read(UC_ARM_REG_R0)
        assert value == expected, (ratio, dc1, dc2, flags, value, expected)
        # Execute event -> collector -> actual R24 byte, without reimplementing it.
        event, collector = 0x20020000, 0x20030000
        u.mem_write(event + 0x220, bytes([0, value]))
        u.reg_write(UC_ARM_REG_R5, event); u.reg_write(UC_ARM_REG_R4, collector)
        u.emu_start((BASE + 0x35f04) | 1, BASE + 0x35f14, count=100)
        u.emu_start((BASE + 0x347f8) | 1, BASE + 0x3480c, count=100)
        assert bytes(u.mem_read(collector + 0x2fc0 + 85, 2)) == bytes([0, value])
        cases.append(dict(ratio=ratio, dc=[dc1, dc2], flags=flags, packet_byte_86=value))
    return dict(firmware_sha256=HASH, cases=cases,
                conclusion='Byte 86 contains SpO2 or a status code. 98 also occurs with missing DC; withhold it.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('firmware', type=Path)
    print(json.dumps(verify(parser.parse_args().firmware.read_bytes()), indent=2))
