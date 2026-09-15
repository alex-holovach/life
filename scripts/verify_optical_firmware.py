"""Execute Harvard 41.17.4.0 optical serialization and rolling pulse gate offline.

Usage: python scripts/verify_optical_firmware.py /path/to/firmware.bin
Requires Unicorn and the exact decompressed firmware; no firmware is distributed.
Only synthetic inputs are used. This checks encoding, not physiological accuracy.
"""
import argparse
import math
import struct
import json
import hashlib
from pathlib import Path
import sys
import zlib
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
from unicorn.arm_const import (UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC, UC_ARM_REG_SP,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
                               UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                               UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_S16,
                               UC_ARM_REG_PC)
from verify_hrv_firmware import BASE, HASH

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from whoop_protocol import decode


def verify(binary):
    if hashlib.sha256(binary).hexdigest() != HASH:
        raise ValueError('Unsupported firmware binary')
    u=Uc(UC_ARCH_ARM,UC_MODE_THUMB);u.mem_map(0x10000000,0x200000);u.mem_write(BASE,binary);u.mem_map(0x20000000,0x80000)
    u.reg_write(UC_ARM_REG_C1_C0_2,0xf00000);u.reg_write(UC_ARM_REG_FPEXC,0x40000000)
    def run(start,end,regs=(),count=500000):
     u.reg_write(UC_ARM_REG_SP,0x2007f000)
     for reg,value in regs:u.reg_write(reg,value)
     u.emu_start((BASE+start)|1,BASE+end,count=count)
     assert u.reg_read(UC_ARM_REG_PC)==BASE+end,hex(u.reg_read(UC_ARM_REG_PC))
    def put(at,fmt,*values):u.mem_write(at,struct.pack('<'+fmt,*values))
    def get(at,fmt):return struct.unpack('<'+fmt,u.mem_read(at,struct.calcsize('<'+fmt)))
    # A single high filtered-signal block stays in the exact rolling gate for 30 calls.
    internal=0x20014000;published=0x20018000;signal=0x20010000;rolling=[]
    for k in range(33):
     samples=[.01]*100
     if k==1:samples[50]=.04
     put(signal,'100f',*samples)
     run(0x87c5e,0x87cd2,[(UC_ARM_REG_R4,internal),(UC_ARM_REG_R5,signal),(UC_ARM_REG_S16,struct.unpack('<I',struct.pack('<f',.01))[0])])
     actual=get(internal+0xe54,'f')[0];expected=.04 if 1<=k<=30 else .01
     assert math.isclose(actual,expected,rel_tol=1e-6),(k,actual)
     put(internal+0xcfc,'I3f',3,.8,.81,.79);put(published+0xe28,'B',0)
     run(0x86862,0x86878,[(UC_ARM_REG_R4,published),(UC_ARM_REG_R6,internal)])
     n=get(published+0xe28,'B')[0];assert n==(0 if expected>.03 else 3)
     rolling.append({'batch':k,'criterion':actual,'published_intervals':n})
    # Serialize the actual R25 waveform path from synthetic source samples.
    ring=0x20020000;source=0x20030000;obj=0x20034000;event=0x20038000;collector=0x20040000
    waveforms=[]
    for index,(shape,values) in enumerate([('ramp',[1000000+100*i for i in range(25)]),('falling',[2000000-137*i for i in range(25)]),('signed',[-2000+50*i for i in range(25)]),('clipped',[1000000+(100000 if i%2 else 0) for i in range(25)])]):
     for addr in [ring,source,obj,event]:u.mem_write(addr,bytes(4096))
     u.mem_write(collector,bytes(0x4000))
     counter=65534+index
     put(source+4,'IH',1700000000,1234);put(source+0x10,'I',counter)
     for i,v in enumerate(values):put(source+0x1c+8*i,'i',v)
     put(obj+0x2c,'f',.0125);put(obj+0x216,'H',2345);put(obj+0x21c,'B',2);put(ring+0xe,'H',4321)
     run(0x8d26a,0x8d2d6,[(UC_ARM_REG_R4,ring),(UC_ARM_REG_R5,source),(UC_ARM_REG_R6,obj),(UC_ARM_REG_R8,source)])
     put(ring+0x829,'B',1)
     run(0x80f00,0x80f6c,[(UC_ARM_REG_R3,ring),(UC_ARM_REG_R9,obj)])
     run(0x604e8,0x604f6,[(UC_ARM_REG_R8,obj+0xe),(UC_ARM_REG_R6,event)])
     run(0x35f8a,0x35f9a,[(UC_ARM_REG_R4,collector),(UC_ARM_REG_R5,event)])
     run(0x34886,0x348a6,[(UC_ARM_REG_R4,collector),(UC_ARM_REG_R2,47)])
     u.reg_write(UC_ARM_REG_R7,collector+0x303f)
     run(0x349bc,0x34a3e,[(UC_ARM_REG_R4,collector)])
     raw=bytes(u.mem_read(collector+0x3028,84));deltas=struct.unpack_from('<24h',raw,23)
     assert struct.unpack_from('<I',raw,7)[0]==counter & 0xffff
     assert struct.unpack_from('<IH',raw,11)==(1700000000,1234)
     assert struct.unpack_from('<H',raw,17)[0]==4321
     assert struct.unpack_from('<i',raw,19)[0]==values[0]
     assert list(deltas)==[max(-32768,min(32767,b-a)) for a,b in zip(values,values[1:])]
     assert math.isclose(struct.unpack_from('<f',raw,71)[0],.0125,rel_tol=1e-6)
     assert struct.unpack_from('<H',raw,75)[0]==2345 and raw[77:79]==bytes([2,1])
     decoded=[struct.unpack_from('<i',raw,19)[0]]
     for delta in deltas:decoded.append(decoded[-1]+delta)
     if shape!='clipped':assert decoded==values
     else:assert decoded!=values
     framed=bytearray(raw);framed[-4:]=zlib.crc32(framed[4:-4]).to_bytes(4,'little')
     wire=decode(bytes(framed))
     assert wire['layout']=='history_optical_r25_candidate'
     assert wire['fields']['sequence_raw']==counter & 0xffff
     assert wire['optical_deltas_raw']==list(deltas)
     if shape!='clipped':assert wire['waveforms']['optical_raw']==values
     else:assert not wire['waveforms'] and wire['waveform_quality']=='clipped_differences'
     waveforms.append({'shape':shape,'input_counter':counter,'packet_counter':counter & 0xffff,'sample_count':len(decoded),'clipped_deltas':sum(d in (-32768,32767) for d in deltas),'reconstructed_exactly':decoded==values})
    return dict(firmware_sha256=HASH, rolling_gate=rolling, r25=waveforms,
                conclusion='Wire encoding verified; channel identity and physiological accuracy remain unvalidated.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('firmware', type=Path)
    print(json.dumps(verify(parser.parse_args().firmware.read_bytes()), indent=2))
