"""Measured collection and analysis availability, never a confidence percentage.

Computed by the analyzer, not on every phone request or Prometheus scrape.
Overlapping accepted windows contribute their union, not five minutes each.
"""
from collections import Counter
import json
import hrv
import respiration
import temperature

KEY = 'analysis_quality_v1'
LOOKBACK = 86400


def union_seconds(spans):
    total=0; right=float('-inf')
    for start,end in sorted(spans):
        total+=max(0,end-max(start,right)); right=max(right,end)
    return total


def window_summary(db,device,table,algorithm,start,end,metric):
    rows=db.execute(f'SELECT result FROM {table} WHERE device=? AND algorithm=? AND end BETWEEN ? AND ? ORDER BY end',
                    (device,algorithm,start+300,end)).fetchall()
    results=[json.loads(row[0]) for row in rows]
    valid=[r for r in results if r['state']=='ready']
    return dict(analyzed_windows=len(results),qualified_windows=len(valid),
                qualified_seconds=union_seconds((max(start,r['start']),min(end,r['end'])) for r in valid),
                reasons=dict(Counter(reason for r in results for reason in set(r['reasons']))),
                points=[dict(at=r['end'],value=r[metric]) for r in valid])


def calculate(db,now):
    start=now-LOOKBACK
    device=db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
    device=device[0] if device else ''
    rows=db.execute('SELECT at,seq,words,conflict FROM hrv_records WHERE device=? AND at BETWEEN ? AND ? ORDER BY at',
                    (device,start-1.05,now)).fetchall()
    through=db.execute('SELECT max(at) FROM hrv_records WHERE device=? AND at<=?',(device,now)).fetchone()[0]
    seconds=0
    for a,b in zip(rows,rows[1:]):
        if not a['conflict'] and not b['conflict'] and .90<=b['at']-a['at']<=1.05 and ((b['seq']-a['seq'])&0xffffffff)==1:
            seconds+=max(0,min(now,b['at'])-max(start,a['at']))
    history=dict(through=through,recorded_seconds=seconds,record_coverage=seconds/LOOKBACK,
                 records=sum(r['at']>=start for r in rows),
                 empty_interval_records=sum(r['at']>=start and not json.loads(r['words']) for r in rows),
                 conflicting_records=sum(r['at']>=start and bool(r['conflict']) for r in rows))
    # Exclude an unfilled trailing window during backfill, using the same cadence
    # tolerance as the metric endpoints. Reports contain actual measurement times.
    end=min(now,through+1.05) if through is not None else start
    return dict(state='ready',computed_at=now,start=start,end=now,history=history,
                hrv=window_summary(db,device,'hrv_windows',hrv.ALGORITHM,start,end,'rmssd_ms'),
                respiration=window_summary(db,device,'respiration_windows',respiration.ALGORITHM,start,end,'breaths_per_minute'))


def save(db,now):
    previous=db.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
    if previous and 0<=now-json.loads(previous[0])['computed_at']<30:return
    report=calculate(db,now)
    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(KEY,json.dumps(report,allow_nan=False)))


def status(db,now):
    base=dict(state='analysis_unavailable')
    device=db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
    if device and temperature.firmware_at(db,device[0],now)!=temperature.FIRMWARE:
        return dict(state='unsupported_firmware')
    heartbeat=db.execute("SELECT value FROM settings WHERE key='hrv_heartbeat'").fetchone()
    row=db.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
    if not heartbeat or not row or not 0<=now-float(heartbeat[0])<180:return base
    report=json.loads(row[0])
    return report if 0<=now-report['computed_at']<180 else base


def metrics(db,now):
    report=status(db,now)
    if report['state']!='ready':return {}
    values={'whoop_history_record_coverage_24h_ratio':report['history']['record_coverage']}
    for key,prefix in [('hrv','whoop_hrv'),('respiration','whoop_respiratory_rate')]:
        summary=report[key]
        values[prefix+'_qualified_windows_24h']=summary['qualified_windows']
        values[prefix+'_analyzed_windows_24h']=summary['analyzed_windows']
        values[prefix+'_qualified_coverage_24h_ratio']=summary['qualified_seconds']/LOOKBACK
    values['whoop_hrv_pair_limited_windows_24h']=report['hrv']['reasons'].get('too_few_adjacent_pairs',0)
    return values
