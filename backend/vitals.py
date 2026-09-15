"""Versioned oxygen decoding and scheduled pulse-derived respiration results."""
import base64
import json
import struct
import respiration
from temperature import FIRMWARE, firmware_at
from whoop_protocol import decode

OXYGEN_DECODER = 'harvard_41_17_4_0_spo2_v1'
SCHEMA = """
CREATE TABLE IF NOT EXISTS oxygen_records(
 device TEXT NOT NULL, at REAL NOT NULL, code INTEGER NOT NULL,
 conflict INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(device,at));
CREATE TABLE IF NOT EXISTS respiration_windows(
 device TEXT NOT NULL,end REAL NOT NULL,algorithm TEXT NOT NULL,result TEXT NOT NULL,
 PRIMARY KEY(device,end,algorithm));
CREATE TABLE IF NOT EXISTS respiration_dirty(
 device TEXT NOT NULL,end REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(device,end));
"""


def oxygen_record(capture):
    if capture.source != 'history': return None
    raw=base64.b64decode(capture.rawBase64);frame=decode(raw)
    if not frame or frame['layout']!='history_v24':return None
    fraction=struct.unpack_from('<H',raw,15)[0]
    at=struct.unpack_from('<I',raw,11)[0]+fraction/32768
    if fraction>=32768 or not 1577836800<=at<=capture.receivedAt+300:return None
    return dict(device=capture.deviceId,at=at,code=raw[86])


def oxygen_value(code):
    if code==0:return None,'awaiting_device_reading'
    # The actual firmware also returns 98 with missing optical DC. The ratio
    # diagnostic is not copied into R24, so a genuine 98 cannot be distinguished.
    if code==98:return None,'ambiguous_firmware_value'
    if 70<=code<=100:return code,'ready'
    return None,'device_signal_unavailable'


def index(db, oxygen, pulse):
    if oxygen:
        db.execute('''INSERT INTO oxygen_records(device,at,code) VALUES(:device,:at,:code)
            ON CONFLICT(device,at) DO UPDATE SET conflict=CASE
            WHEN oxygen_records.code=excluded.code THEN oxygen_records.conflict ELSE 1 END''',oxygen)
    if pulse:
        first=(int(pulse['at'])//60+1)*60
        db.executemany('''INSERT INTO respiration_dirty(device,end) VALUES(?,?)
            ON CONFLICT(device,end) DO UPDATE SET revision=revision+1''',
            [(pulse['device'],end) for end in range(first,first+300,60)])


def invalidate_firmware(db,now):
    db.execute('''INSERT INTO respiration_dirty(device,end)
        SELECT device,end FROM respiration_windows WHERE end>?
        ON CONFLICT(device,end) DO UPDATE SET revision=revision+1''',(now-30*86400,))


def analyze(connect,now):
    # Numerical analysis must not hold SQLite's write lock while ingestion runs.
    with connect() as db:
        dirty=db.execute('SELECT * FROM respiration_dirty WHERE end<=? ORDER BY end DESC LIMIT 30',(now-10,)).fetchall()
    for item in dirty:
        device,end,revision=tuple(item)
        with connect() as db:
            rows=db.execute('SELECT * FROM hrv_records WHERE device=? AND at>=? AND at<? ORDER BY at',(device,end-300,end)).fetchall()
            versions={r[0] for r in db.execute('SELECT firmware FROM firmware_observations WHERE device=? AND observed_at>? AND observed_at<?',(device,end-300,end))}
            versions.add(firmware_at(db,device,end-300))
        result=respiration.calculate(rows,end)
        if versions!={FIRMWARE}:
            result.update(state='unsupported_firmware',breaths_per_minute=None,quality='unavailable')
            result['reasons'].append('firmware_not_verified_for_window')
        with connect() as db:
            db.execute('INSERT OR REPLACE INTO respiration_windows VALUES(?,?,?,?)',(device,end,respiration.ALGORITHM,json.dumps(result,allow_nan=False)))
            db.execute('DELETE FROM respiration_dirty WHERE device=? AND end=? AND revision=?',(device,end,revision))
    return len(dirty)


def oxygen_status(db,device,now):
    result=dict(state='awaiting_history',percent=None,observed_at=None,
                decoder=OXYGEN_DECODER,quality='device_calculated',validation='not_reference_validated',latest_attempt=None)
    latest=db.execute('SELECT * FROM oxygen_records WHERE device=? AND at BETWEEN ? AND ? ORDER BY at DESC LIMIT 1',(device,now-86400,now)).fetchone()
    if latest:
        _,state=oxygen_value(latest['code'])
        if latest['conflict']:state='conflicting_record'
        if firmware_at(db,device,latest['at'])!=FIRMWARE:state='unsupported_firmware'
        result.update(state=state,latest_attempt=dict(state=state,observed_at=latest['at'],raw_code=latest['code']))
    candidates=db.execute('''SELECT at,code FROM oxygen_records WHERE device=? AND at BETWEEN ? AND ?
        AND code BETWEEN 70 AND 100 AND code!=98 AND conflict=0 ORDER BY at DESC''',(device,now-86400,now))
    for at,code in candidates:
        if firmware_at(db,device,at)==FIRMWARE:
            result.update(state='ready',percent=code,observed_at=at);break
    return result


def respiration_status(db,device,now):
    history_end=db.execute('SELECT max(at) FROM hrv_records WHERE device=? AND at<=?',(device,now)).fetchone()[0]
    base=dict(state='awaiting_history',breaths_per_minute=None,end=None,start=None,
              algorithm=respiration.ALGORITHM,quality='unavailable',validation='not_reference_validated',
              latest_attempt=None,duration_seconds=0,coverage=0,spectral_power=0,reasons=[],history_end=history_end)
    rows=db.execute('SELECT result FROM respiration_windows WHERE device=? AND algorithm=? AND end BETWEEN ? AND ? ORDER BY end DESC',
                    (device,respiration.ALGORITHM,now-86400,min(now,history_end+1.05) if history_end is not None else now)).fetchall()
    if not rows:return base
    results=[json.loads(r[0]) for r in rows]
    # Overlapping windows may repeat the same underlying interval segment.
    # Freshness is always measured from that segment, never the analysis tick.
    valid=max((r for r in results if r['state']=='ready' and 0<=now-r['end']<86400),
              key=lambda r:r['end'],default=None)
    if valid:return valid | dict(state='ready' if now-valid['end']<=900 else 'stale',latest_attempt=results[0],history_end=history_end)
    latest=results[0] | dict(latest_attempt=results[0],history_end=history_end)
    if latest['state']=='ready':latest.update(state='stale',breaths_per_minute=None)
    return latest


def status(db,now):
    row=db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
    device=row[0] if row else ''
    result=dict(oxygen=oxygen_status(db,device,now),respiration=respiration_status(db,device,now))
    heartbeat=db.execute("SELECT value FROM settings WHERE key='hrv_heartbeat'").fetchone()
    state=None
    if not row:state='awaiting_history'
    elif firmware_at(db,device,now)!=FIRMWARE:state='unsupported_firmware'
    elif not heartbeat or not 0<=now-float(heartbeat[0])<180:state='analysis_unavailable'
    if state:
        result['oxygen'].update(state=state,percent=None)
        result['respiration'].update(state=state,breaths_per_minute=None)
    return result


def respiration_signal_status(result):
    """Bounded dashboard status codes; no pulse-quality threshold is changed."""
    if result['state']=='ready':return 1
    if result['state']=='stale':return 2
    if result['state']=='analysis_unavailable':return 7
    if result['state']=='unsupported_firmware':return 8
    reasons=result.get('reasons',[])
    if any(r in reasons for r in ('short_contiguous_segment','no_contiguous_intervals')):return 3
    if 'incomplete_pulse_coverage' in reasons:return 4
    if 'pulse_artifacts' in reasons or 'changing_heart_rate' in reasons:return 5
    if reasons:return 6
    return 0


def metrics(db,now):
    s=status(db,now);o=s['oxygen'];r=s['respiration']
    values={'whoop_spo2_ready':int(o['state']=='ready'),'whoop_respiratory_rate_ready':int(r['state']=='ready')}
    values['whoop_respiratory_rate_signal_status']=respiration_signal_status(r)
    if o['latest_attempt']:
        values.update(whoop_spo2_raw_code=o['latest_attempt']['raw_code'],
                      whoop_spo2_last_history_timestamp_seconds=o['latest_attempt']['observed_at'])
    if o['state']=='ready':
        values.update(whoop_spo2_percent=o['percent'],whoop_spo2_observed_timestamp_seconds=o['observed_at'])
    if r['latest_attempt']:
        values['whoop_respiratory_rate_contiguous_seconds']=r['latest_attempt']['duration_seconds']
    if r['state']=='ready':
        values.update(whoop_respiratory_rate_estimated_breaths_per_minute=r['breaths_per_minute'],
                      whoop_respiratory_rate_window_end_timestamp_seconds=r['end'],
                      whoop_respiratory_rate_spectral_power_ratio=r['spectral_power'])
    if r['state'] in ('ready','stale') and r['breaths_per_minute'] is not None:
        values.update(whoop_respiratory_rate_last_qualified_breaths_per_minute=r['breaths_per_minute'],
                      whoop_respiratory_rate_last_qualified_end_timestamp_seconds=r['end'])
    return values
