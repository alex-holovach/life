"""Offline checks against the exact Harvard 41.17.4.0 ARM firmware.

Usage: python scripts/verify_hrv_firmware.py /path/to/harvard-41.17.4.decompressed.bin
Requires Unicorn. No device access; the proprietary firmware is not distributed.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
from unicorn.arm_const import (UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC, UC_ARM_REG_SP,
                              UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                              UC_ARM_REG_R4, UC_ARM_REG_R6, UC_ARM_REG_LR, UC_ARM_REG_PC)

HASH='2c4d7a9edf90e4b39bcecda28d2cd9949bd19de533a14d31a7409b31144c309b'
BASE=0x10028000


def publication_limits(u):
    """Counterexamples to a timing-only continuity rule, not a new decoder."""
    source=0x20030000;output=0x20032000;state=0x20034000;stop=0x10029000
    cases=[]
    for bpm,shape,batches in [(45,'changing_width',180),(100,'sine',340)]:
        u.mem_write(output,bytes(4096));u.mem_write(state,bytes(4096))
        previous=None;elapsed=0;phases=[];records=[];flagged=0
        for k in range(batches):
            signal=[]
            for i in range(400):
                t=(i+100*k)/104
                value=math.sin(2*math.pi*bpm/60*t)
                if shape=='changing_width':
                    power=1+4*(.5+.5*math.sin(t*.05))
                    value=math.copysign(abs(value)**power,value)
                signal.append(value)
            u.mem_write(source,struct.pack('<400f',*signal));u.reg_write(UC_ARM_REG_SP,0x2007f000)
            for reg,value in [(UC_ARM_REG_R0,source),(UC_ARM_REG_R1,400),(UC_ARM_REG_R2,output),
                              (UC_ARM_REG_R3,state),(UC_ARM_REG_LR,stop|1)]:u.reg_write(reg,value)
            u.emu_start((BASE+0x8a374)|1,stop,count=500000)
            assert u.reg_read(UC_ARM_REG_PC)==stop, 'Peak detector exceeded instruction budget'
            n=struct.unpack('<I',u.mem_read(output,4))[0]
            intervals=list(struct.unpack('<'+'f'*n,u.mem_read(output+4,n*4)))
            flagged+=bool(u.mem_read(output+0xb4,1)[0])
            records.append([int(x*1000+.5) for x in intervals])
            if not n:continue
            peaks=[x+100*k for x in struct.unpack('<'+'I'*(n+1),u.mem_read(output+0x3c,4*(n+1)))]
            if previous is not None:assert peaks[0]==previous
            previous=peaks[-1];elapsed+=sum(intervals)
            if k>=15:phases.append(k*100/104-elapsed)
        case=dict(bpm=bpm,shape=shape,batches=batches,flagged_batches=flagged,
                  phase_spread_seconds=max(phases)-min(phases))
        if shape=='changing_width':
            # All emitted endpoints are continuous, yet morphology changes the
            # publication delay by more than cadence plus interpolation alone.
            assert flagged==0
            assert case['phase_spread_seconds']>102/104
        else:
            rows=records[15:327]
            def phase_compatible(rows):
                total=0;count=0;phases=[]
                for k,words in enumerate(rows):
                    total+=sum(words)/1000;count+=len(words)
                    if words:phases.append(k*100/104-total)
                return max(phases)-min(phases)<=102/104+count*.0005
            assert phase_compatible(rows)
            faults={}
            for kind in ['withheld','duplicate','clipped','corrupted']:
                tested=compatible=range_valid=0
                for i in range(10,len(rows)-10):
                    if not rows[i]:continue
                    changed=[list(r) for r in rows]
                    if kind=='withheld':changed[i].pop(0)
                    elif kind=='duplicate':changed[i].append(changed[i][0])
                    elif kind=='clipped':changed[i][0]=500
                    else:changed[i][0]+=60
                    tested+=1;fits=phase_compatible(changed);compatible+=fits
                    range_valid+=fits and all(333<x<2000 and x!=500 for r in changed for x in r)
                faults[kind]=dict(injections=tested,phase_compatible=compatible,
                                  phase_and_range_compatible=range_valid)
            assert faults['withheld']['phase_and_range_compatible']>0
            assert faults['duplicate']['phase_and_range_compatible']>0
            assert faults['clipped']['phase_and_range_compatible']==0
            assert faults['corrupted']['phase_and_range_compatible']>0
            case['faults']=faults
        cases.append(case)
    # Execute the actual output gate on controlled intermediate state. The full
    # caller zeros the output before this branch; output suppression does not
    # undo the earlier peak-detector cursor advancement.
    gating=[];internal=0x20014000;published=0x20018000
    for criterion in [0.0,0.029,0.03,0.031,1.0]:
        u.mem_write(internal,bytes(4096));u.mem_write(published,bytes(4096))
        u.mem_write(internal+0xcfc,struct.pack('<I3f',3,.8,.81,.79))
        u.mem_write(internal+0xe54,struct.pack('<f',criterion))
        u.reg_write(UC_ARM_REG_R4,published);u.reg_write(UC_ARM_REG_R6,internal)
        u.emu_start((BASE+0x86862)|1,BASE+0x86878,count=500)
        assert u.reg_read(UC_ARM_REG_PC)==BASE+0x86878, 'Output gate exceeded instruction budget'
        n=u.mem_read(published+0xe28,1)[0]
        assert n==(3 if criterion<.03 else 0)
        gating.append(dict(internal_criterion=criterion,published_intervals=n))
    return dict(synthetic_cases=cases,output_gate=gating,
                conclusion='Phase compatibility cannot prove uninterrupted intervals. No production quality gate changed.')


def verify(binary):
    if hashlib.sha256(binary).hexdigest()!=HASH:raise ValueError('Unsupported firmware binary')
    u=Uc(UC_ARCH_ARM,UC_MODE_THUMB)
    u.mem_map(0x10000000,0x200000);u.mem_write(BASE,binary);u.mem_map(0x20000000,0x80000)
    u.reg_write(UC_ARM_REG_C1_C0_2,0xf00000);u.reg_write(UC_ARM_REG_FPEXC,0x40000000)
    u.reg_write(UC_ARM_REG_SP,0x2007f000)
    obj=0x20010000;algo=0x2004b2d0;collector=0x20020000
    packing=[]
    for values in [[],[1.0],[.876,.921],[.800,.810,.790,.805],[.90049,.90051],[.6,.7,.8,.9,1.0]]:
        u.mem_write(algo+0xe28,bytes([len(values)]))
        u.mem_write(algo+0xe10,struct.pack('<'+'f'*len(values),*values))
        u.reg_write(UC_ARM_REG_R3,algo);u.reg_write(UC_ARM_REG_R4,obj)
        u.emu_start((BASE+0x7f684)|1,BASE+0x7f6d0,count=500)
        packed=bytes(u.mem_read(obj+0xe,9));n=packed[0]
        actual=list(struct.unpack_from('<'+'H'*n,packed,1))
        expected=[int(x*1000+.5) for x in values[:4]]
        assert actual==expected,(values,actual,expected)
        u.mem_write(collector+0x154,packed);u.reg_write(UC_ARM_REG_R4,collector)
        u.emu_start((BASE+0x3472e)|1,BASE+0x34746,count=100)
        assert bytes(u.mem_read(collector+0x2fc0+22,9))==packed
        packing.append(dict(seconds=values,packet_intervals_ms=actual))
    # Execute the complete peak detector with a sliding 400-sample signal. It
    # advances 100 samples at 104 Hz. Interpolated peaks remain in temporal order;
    # the final peak of one call is the initial peak of the next nonempty call.
    source=0x20030000;output=0x20032000;state=0x20034000;stop=0x10029000
    previous=None;windows=[]
    for k in range(40):
        signal=[math.sin(2*math.pi*((i+100*k)/104+.03*math.sin((i+100*k)/200))) for i in range(400)]
        u.mem_write(source,struct.pack('<400f',*signal));u.reg_write(UC_ARM_REG_SP,0x2007f000)
        for reg,value in [(UC_ARM_REG_R0,source),(UC_ARM_REG_R1,400),(UC_ARM_REG_R2,output),
                          (UC_ARM_REG_R3,state),(UC_ARM_REG_LR,stop|1)]:u.reg_write(reg,value)
        u.emu_start((BASE+0x8a374)|1,stop,count=500000)
        n=struct.unpack('<I',u.mem_read(output,4))[0]
        intervals=list(struct.unpack('<'+'f'*n,u.mem_read(output+4,n*4)))
        peaks=[x+100*k for x in struct.unpack('<'+'I'*(n+1),u.mem_read(output+0x3c,4*(n+1)))]
        if n:
            assert all(b>a for a,b in zip(peaks,peaks[1:]))
            if previous is not None:assert peaks[0]==previous,(k,peaks[0],previous)
            # Peak interpolation shifts endpoints by less than one sample each.
            assert all(abs(seconds-(b-a)/104)<2/104 for seconds,a,b in zip(intervals,peaks,peaks[1:]))
            assert all(.98<x<1.02 for x in intervals)
            previous=peaks[-1]
        windows.append(dict(batch=k,interval_count=n))
    assert any(w['interval_count']==0 for w in windows)
    # Natural empty batches become common below the record publication rate.
    # Keep this reproducible with firmware execution, rather than inferring that
    # every empty packet means either data loss or a continuous signal.
    clean_signal_cases=[]
    for bpm in [45,50,55,60,75,100]:
        u.mem_write(output,bytes(4096));u.mem_write(state,bytes(4096))
        previous=None;empty=0;continuity_checks=0;emitted=[];phases=[]
        for k in range(120):
            signal=[math.sin(2*math.pi*(bpm/60*(i+100*k)/104+.01*math.sin((i+100*k)/200))) for i in range(400)]
            u.mem_write(source,struct.pack('<400f',*signal));u.reg_write(UC_ARM_REG_SP,0x2007f000)
            for reg,value in [(UC_ARM_REG_R0,source),(UC_ARM_REG_R1,400),(UC_ARM_REG_R2,output),
                              (UC_ARM_REG_R3,state),(UC_ARM_REG_LR,stop|1)]:u.reg_write(reg,value)
            u.emu_start((BASE+0x8a374)|1,stop,count=500000)
            n=struct.unpack('<I',u.mem_read(output,4))[0]
            if not n:empty+=1;continue
            peaks=[x+100*k for x in struct.unpack('<'+'I'*(n+1),u.mem_read(output+0x3c,4*(n+1)))]
            assert all(b>a for a,b in zip(peaks,peaks[1:]))
            if previous is not None:
                assert peaks[0]==previous,(bpm,k,peaks[0],previous)
                continuity_checks+=1
            previous=peaks[-1]
            emitted.extend(struct.unpack('<'+'f'*n,u.mem_read(output+4,n*4)))
            phases.append(k*100/104-sum(emitted))
        mean_interval=sum(emitted)/len(emitted)
        assert abs(mean_interval-60/bpm)<.01
        if bpm<60:assert empty>0
        phase_spread=max(phases)-min(phases)
        assert phase_spread<100/104+2/104
        clean_signal_cases.append(dict(bpm=bpm,batches=120,empty_batches=empty,
                                       cursor_continuity_checks=continuity_checks,
                                       publication_phase_spread_seconds=phase_spread))
    return dict(firmware_sha256=HASH,packing=packing,sliding_windows=windows,
                clean_signal_cases=clean_signal_cases,
                publication_limits=publication_limits(u),
                conclusion='Milliseconds; chronological; no repeated interval across synthetic batches. Not an ECG validation.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('firmware',type=Path)
    print(json.dumps(verify(parser.parse_args().firmware.read_bytes()),indent=2))
