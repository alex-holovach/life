"""Five-minute PPG RMSSD from firmware-proven, ordered R24 millisecond intervals.

Quality thresholds are conservative product rules, not a clinical validation.
See docs/HRV.md. Never concatenate across empty records, missing sequence numbers,
rejected beats, clock discontinuities, or conflicting records. Never interpolate.
"""
import base64
import json
import math
import statistics
import struct
from whoop_protocol import decode
from temperature import FIRMWARE, firmware_at

ALGORITHM = 'r24_rmssd_5m_v1'
FRESH_SECONDS = 900
SCHEMA = """
CREATE TABLE IF NOT EXISTS hrv_records(
 device TEXT NOT NULL, at REAL NOT NULL, seq INTEGER NOT NULL,
 words TEXT NOT NULL, hr INTEGER NOT NULL, conflict INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(device,at));
CREATE TABLE IF NOT EXISTS hrv_windows(
 device TEXT NOT NULL, end REAL NOT NULL, algorithm TEXT NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(device,end,algorithm));
CREATE TABLE IF NOT EXISTS hrv_dirty(device TEXT NOT NULL,end REAL NOT NULL,PRIMARY KEY(device,end));
"""


def record(capture):
    if capture.source != 'history': return None
    raw = base64.b64decode(capture.rawBase64)
    frame = decode(raw)
    if not frame or frame['layout'] != 'history_v24': return None
    fraction = struct.unpack_from('<H',raw,15)[0]
    at = struct.unpack_from('<I',raw,11)[0] + fraction/32768
    if fraction >= 32768 or not 1577836800 <= at <= capture.receivedAt+300: return None
    return dict(device=capture.deviceId, at=at, seq=struct.unpack_from('<I',raw,7)[0],
                words=list(struct.unpack_from('<'+'H'*raw[22],raw,23)), hr=raw[21], conflict=0)


def index(db, row):
    values=(row['device'],row['at'],row['seq'],json.dumps(row['words']),row['hr'])
    old=db.execute('SELECT seq,words,hr FROM hrv_records WHERE device=? AND at=?',values[:2]).fetchone()
    if old:
        if tuple(old) == values[2:]: return
        db.execute('UPDATE hrv_records SET conflict=1 WHERE device=? AND at=?',values[:2])
    else:
        db.execute('INSERT INTO hrv_records(device,at,seq,words,hr) VALUES(?,?,?,?,?)',values)
    # A record contributes to five overlapping windows ending on UTC minutes.
    first=(int(row['at'])//60+1)*60
    db.executemany('INSERT OR IGNORE INTO hrv_dirty VALUES(?,?)',
                   [(row['device'],end) for end in range(first,first+300,60)])


def calculate(rows, end):
    start=end-300
    rows=sorted((dict(r) for r in rows if start <= r['at'] < end),key=lambda r:r['at'])
    result=dict(state='insufficient_data',rmssd_ms=None,start=start,end=end,
                algorithm=ALGORITHM,source='ppg',intervals=0,accepted_intervals=0,
                pairs=0,coverage=0.0,rejected_fraction=0.0,record_coverage=0.0,
                reasons=[],precision='1 ms encoding; physiological accuracy not ECG-validated')
    if not rows:
        result['reasons']=['no_history'];return result
    beats=[];previous=None;segment=0;record_gaps=0
    for row in rows:
        words=json.loads(row['words']) if isinstance(row['words'],str) else row['words']
        contiguous=previous is not None and .90 <= row['at']-previous['at'] <= 1.05 and ((row['seq']-previous['seq']) & 0xffffffff)==1
        if not contiguous:
            segment+=1
            if previous is not None: record_gaps+=1
        if row.get('conflict'):
            result['reasons'].append('conflicting_record')
            words=[]
        if not words:
            segment+=1
        for value in words:
            beats.append((value,segment,row['hr']))
        previous=row
    result['record_coverage']=min(1,len(rows)*(100/104)/300)
    result['intervals']=len(beats)
    if not beats:
        result['reasons'].append('no_pulse_intervals');return result
    # Conservative outlier detection, applied to original positions. Exclusion
    # never creates new neighboring pairs. Clamp values from firmware are excluded.
    accepted=[]
    for i,(value,seg,hr) in enumerate(beats):
        neighbors=[b[0] for b in beats[max(0,i-5):i+6] if b[1]==seg and 333 < b[0] < 2000 and b[0]!=500]
        median=statistics.median(neighbors) if neighbors else value
        accepted.append(333 < value < 2000 and value!=500 and hr>0 and abs(value-median)<=median*.20)
    count=sum(accepted)
    result['accepted_intervals']=count
    result['coverage']=sum(b[0] for b,ok in zip(beats,accepted) if ok)/300000
    result['rejected_fraction']=1-count/len(beats)
    differences=[(beats[i][0]-beats[i-1][0])**2 for i in range(1,len(beats))
                 if accepted[i] and accepted[i-1] and beats[i][1]==beats[i-1][1]]
    result['pairs']=len(differences)
    if record_gaps or rows[0]['at']-start>1.05 or end-rows[-1]['at']>1.05:
        result['reasons'].append('missing_history_records')
    if not .98 <= result['coverage'] <= 1.02: result['reasons'].append('incomplete_pulse_coverage')
    if result['rejected_fraction']>.01: result['reasons'].append('pulse_artifacts')
    if len(differences)<180 or len(differences)<.90*max(1,len(beats)-1): result['reasons'].append('too_few_adjacent_pairs')
    # Stable HR across minute blocks is a resting-window screen, not activity classification.
    means=[]
    for minute in range(5):
        rates=[r['hr'] for r in rows if start+minute*60 <= r['at'] < start+(minute+1)*60 and r['hr']>0]
        if rates: means.append(statistics.mean(rates))
    if len(means)!=5 or max(means)-min(means)>10: result['reasons'].append('changing_heart_rate')
    result['reasons']=sorted(set(result['reasons']))
    if not result['reasons']:
        result.update(state='ready',rmssd_ms=math.sqrt(math.fsum(differences)/len(differences)))
    return result


def compute_window(db, device, end):
    rows=db.execute('SELECT * FROM hrv_records WHERE device=? AND at>=? AND at<? ORDER BY at',
                    (device,end-300,end)).fetchall()
    result=calculate(rows,end)
    versions={r[0] for r in db.execute('SELECT firmware FROM firmware_observations WHERE device=? AND observed_at>? AND observed_at<?',
                                     (device,end-300,end))}
    versions.add(firmware_at(db,device,end-300))
    if versions != {FIRMWARE}:
        result.update(state='unsupported_firmware',rmssd_ms=None)
        result['reasons'].append('firmware_not_verified_for_window')
    db.execute('INSERT OR REPLACE INTO hrv_windows VALUES(?,?,?,?)',(device,end,ALGORITHM,json.dumps(result,allow_nan=False)))
    return result


def status(db,now):
    base=dict(state='awaiting_history',rmssd_ms=None,end=None,start=None,algorithm=ALGORITHM,
              source='ppg',coverage=0.0,pairs=0,reasons=[],latest_attempt=None)
    device=db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
    if not device:return base
    history_end=db.execute('SELECT max(at) FROM hrv_records WHERE device=? AND at<=?',(device[0],now)).fetchone()[0]
    base['history_end']=history_end
    if firmware_at(db,device[0],now)!=FIRMWARE:
        return base | dict(state='unsupported_firmware')
    heartbeat=db.execute("SELECT value FROM settings WHERE key='hrv_heartbeat'").fetchone()
    if not heartbeat or not 0 <= now-float(heartbeat[0]) < 180:
        return base | dict(state='analysis_unavailable')
    rows=db.execute('SELECT result FROM hrv_windows WHERE device=? AND algorithm=? AND end>? AND end<=? ORDER BY end DESC',
                    (device[0],ALGORITHM,now-86400,min(now,history_end+1.05) if history_end is not None else now)).fetchall()
    results=[json.loads(r[0]) for r in rows]
    if not results:return base
    latest=results[0]
    valid=next((r for r in results if r['state']=='ready'),None)
    if valid:
        return valid | dict(state='ready' if now-valid['end']<=FRESH_SECONDS else 'stale',
                            latest_attempt=latest,history_end=history_end)
    return latest | dict(latest_attempt=latest,history_end=history_end)


def metrics(db,now):
    s=status(db,now)
    values={'whoop_hrv_ready':int(s['state']=='ready')}
    if s.get('history_end') is not None:
        values['whoop_history_last_sample_timestamp_seconds']=s['history_end']
    values['whoop_analysis_pending_windows']=sum(db.execute(f'SELECT count(*) FROM {table} WHERE end<=?',(now-10,)).fetchone()[0]
                                                for table in ('hrv_dirty','respiration_dirty'))
    latest=s.get('latest_attempt')
    if latest:
        values.update(whoop_hrv_latest_window_coverage_ratio=latest['coverage'],
                      whoop_hrv_latest_window_end_timestamp_seconds=latest['end'])
    if s['state']=='ready':
        values.update(whoop_hrv_rmssd_ms=s['rmssd_ms'],whoop_hrv_window_end_timestamp_seconds=s['end'],
                      whoop_hrv_coverage_ratio=s['coverage'],whoop_hrv_adjacent_pairs=s['pairs'])
    # A resting measurement is useful after its live freshness window. Keep its
    # original timestamp and separate metrics so it cannot masquerade as live HRV.
    if s['state'] in ('ready','stale') and s['rmssd_ms'] is not None:
        values.update(whoop_hrv_last_qualified_rmssd_ms=s['rmssd_ms'],
                      whoop_hrv_last_qualified_end_timestamp_seconds=s['end'])
    return values
