"""Pair a manually measured skin temperature with archived WHOOP words.

Example inside the API container:
 python temperature_reference.py --at 2026-09-14T05:30:00Z --celsius 32.8 --instrument 'Contact probe model'
Wait for stored history to reach the reference time before running. Output is a private
reference record, not a published WHOOP measurement or an activated calibration.
"""
import argparse
import datetime
import json
import math
import os
import statistics
import storage
import temperature


def pair(db, at, celsius, instrument):
    if not math.isfinite(at) or not math.isfinite(celsius) or not 15 <= celsius <= 45 or not instrument.strip():
        raise ValueError('A finite wrist-skin reading and reference instrument are required')
    fields = {}
    for offset in temperature.OFFSETS:
        rows = db.execute('SELECT timestamp,value FROM samples WHERE series=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp',
                          (temperature.metric(temperature.RAW_METRIC,field=f'v24_{offset}'),int((at-30)*1000),int((at+30)*1000))).fetchall()
        if len(rows)<30 or rows[0][0]/1000>at-20 or rows[-1][0]/1000<at+20:
            raise ValueError('History has not covered the reference window yet')
        if max(b[0]-a[0] for a,b in zip(rows,rows[1:]))>2000:
            raise ValueError('Reference window contains missing sensor records')
        values=[r[1] for r in rows]
        fields[str(offset)]=dict(raw=statistics.median(values),min=min(values),max=max(values),samples=len(values))
    return dict(at=at,celsius=celsius,reference_instrument=instrument,fields=fields,window_seconds=60)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--at',required=True,help='ISO timestamp including timezone')
    parser.add_argument('--celsius',type=float,required=True)
    parser.add_argument('--instrument',required=True)
    args=parser.parse_args()
    instant=datetime.datetime.fromisoformat(args.at.replace('Z','+00:00'))
    if instant.tzinfo is None:parser.error('--at must include its timezone')
    with storage.connect() as db:
        reference=pair(db,instant.timestamp(),args.celsius,args.instrument)
    target=storage.directory()/'temperature-references.jsonl'
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'w') as output:
        output.write(json.dumps(reference)+'\n');output.flush();os.fsync(output.fileno())
    print(json.dumps(reference,indent=2))
