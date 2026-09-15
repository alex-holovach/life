import base64
import json
import math
import struct
import time
from types import SimpleNamespace
import pytest
import storage
import hrv
import hrv_analysis
from test_protocol import seal
from test_temperature import HISTORY, firmware
import temperature


def series(end, intervals=None):
    # Generate a continuous 104 Hz, 100-sample record cadence and a known beat clock.
    intervals=intervals or [950,970]
    at=end-300;beat=at+.1;i=0;rows=[];seq=50
    while at<end:
        words=[]
        while beat<=at:
            value=intervals[i%len(intervals)];words.append(value);i+=1;beat+=value/1000
        rows.append(dict(device='test',at=at,seq=seq,words=words,hr=63,conflict=0))
        at+=100/104;seq+=1
    return rows


def test_exact_rmssd_uses_ordered_intervals():
    r=hrv.calculate(series(1800000300),1800000300)
    assert r['state']=='ready',r
    assert r['rmssd_ms']==20
    assert r['coverage']>=.98 and r['pairs']>290


def test_gap_and_duplicate_sequence_do_not_form_pairs():
    rows=series(1800000300);original=hrv.calculate(rows,1800000300)
    del rows[100]
    r=hrv.calculate(rows,1800000300)
    assert r['rmssd_ms'] is None and 'missing_history_records' in r['reasons']
    assert r['pairs']<original['pairs']
    rows=series(1800000300);rows[100]['seq']=rows[99]['seq']
    assert 'missing_history_records' in hrv.calculate(rows,1800000300)['reasons']


def test_empty_and_rejected_intervals_never_create_new_adjacency():
    rows=series(1800000300);baseline=hrv.calculate(rows,1800000300)
    rows[10]['words']=[]
    empty=hrv.calculate(rows,1800000300)
    assert empty['pairs']==baseline['pairs']-2
    rows=series(1800000300);rows[10]['words']=[1900]
    rejected=hrv.calculate(rows,1800000300)
    assert rejected['pairs']==baseline['pairs']-2
    assert rejected['accepted_intervals']==baseline['accepted_intervals']-1


@pytest.mark.parametrize('intervals',[[333],[500],[2000],[2400],[960,100,960,1800]])
def test_clamped_and_artifact_windows_are_not_published(intervals):
    assert hrv.calculate(series(1800000300,intervals),1800000300)['rmssd_ms'] is None


def test_sparse_intervals_and_changing_heart_rate_are_rejected():
    rows=series(1800000300)
    for row in rows[::3]:row['words']=[]
    assert 'incomplete_pulse_coverage' in hrv.calculate(rows,1800000300)['reasons']
    rows=series(1800000300)
    for row in rows[:70]:row['hr']=100
    assert 'changing_heart_rate' in hrv.calculate(rows,1800000300)['reasons']


def test_sequence_rollover_and_input_order():
    rows=series(1800000300)
    for i,row in enumerate(rows):row['seq']=(0xffffffff-100+i)&0xffffffff
    assert hrv.calculate(list(reversed(rows)),1800000300)['rmssd_ms']==20


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));storage.initialize()
    with storage.connect() as db:yield db


def test_index_deduplicates_and_conflicts_are_gated(db):
    row=series(1800000300)[10]
    hrv.index(db,row);hrv.index(db,row)
    assert db.execute('SELECT count(*) FROM hrv_records').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM hrv_dirty').fetchone()[0]==5
    hrv.index(db,row | dict(words=[800]))
    assert db.execute('SELECT conflict FROM hrv_records').fetchone()[0]==1


def test_firmware_temporal_gate_and_freshness(db):
    end=1800000300
    db.execute("INSERT INTO settings VALUES('device','test')")
    for row in series(end):hrv.index(db,row)
    assert hrv.compute_window(db,'test',end)['rmssd_ms'] is None
    temperature.record_firmware(firmware(receivedAt=end-360),db)
    assert hrv.compute_window(db,'test',end)['rmssd_ms']==20
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(end+10),))
    assert hrv.status(db,end+10)['state']=='ready'
    assert hrv.metrics(db,end+10)['whoop_hrv_rmssd_ms']==20
    db.execute("UPDATE settings SET value=? WHERE key='hrv_heartbeat'",(str(end+1000),))
    assert hrv.status(db,end+1000)['state']=='stale'
    assert 'whoop_hrv_rmssd_ms' not in hrv.metrics(db,end+1000)
    db.execute("INSERT INTO firmware_observations VALUES('test',?,'unknown','other')",(end-10,))
    assert hrv.compute_window(db,'test',end)['rmssd_ms'] is None
    assert hrv.status(db,end+10)['state']=='unsupported_firmware'


def test_crc_layout_and_clock_gate():
    raw=bytearray(HISTORY);struct.pack_into('<I',raw,11,1800000000);raw[22]=2
    struct.pack_into('<2H',raw,23,876,921)
    c=SimpleNamespace(deviceId='test',source='history',receivedAt=1800000001,rawBase64=base64.b64encode(seal(raw)))
    assert hrv.record(c)['words']==[876,921]
    raw[23]^=1;c.rawBase64=base64.b64encode(raw)
    assert hrv.record(c) is None
    c.rawBase64=base64.b64encode(seal(raw));c.source='live_hr'
    assert hrv.record(c) is None
    c.source='history';c.receivedAt=1700000000
    assert hrv.record(c) is None


def test_incremental_archive_job_and_late_backfill(tmp_path,monkeypatch):
    import gzip
    monkeypatch.setenv('DATA_DIR',str(tmp_path));storage.initialize();end=(int(time.time())//60)*60
    rows=series(end)
    def upload(items,prefix):
        captures=[]
        for i,row in enumerate(items):
            raw=bytearray(HISTORY);struct.pack_into('<I',raw,7,row['seq']);struct.pack_into('<I',raw,11,int(row['at']))
            struct.pack_into('<H',raw,15,round((row['at']%1)*32768));raw[21]=row['hr'];raw[22]=len(row['words'])
            for j,value in enumerate(row['words']):struct.pack_into('<H',raw,23+j*2,value)
            captures.append(dict(id=f'{prefix}{i}',deviceId='test',source='history',receivedAt=end+5,rawBase64=base64.b64encode(seal(raw)).decode()))
        path=prefix+'.gz'
        with gzip.open(tmp_path/path,'wt') as f:json.dump({'captures':captures},f)
        with storage.connect() as db:
            for i,c in enumerate(captures):db.execute('INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,?)',('test',c['id'],'digest',path,i,end+5,'history',None,None))
    upload(rows[:100]+rows[101:],'initial')
    with storage.connect() as db:
        temperature.record_firmware(firmware(receivedAt=end-360),db)
        # Upgrading an already-indexed HRV installation must replay oxygen too.
        db.execute("INSERT INTO settings VALUES('hrv_archive_cursor','999999')")
    assert hrv_analysis.run_once(end+20)[0]==len(rows)-1
    with storage.connect() as db:
        result=json.loads(db.execute('SELECT result FROM hrv_windows WHERE end=?',(end,)).fetchone()[0]);assert result['rmssd_ms'] is None
    upload([rows[100]],'late')
    assert hrv_analysis.run_once(end+30)[0]==1
    with storage.connect() as db:
        result=json.loads(db.execute('SELECT result FROM hrv_windows WHERE end=?',(end,)).fetchone()[0]);assert result['rmssd_ms']==20
        assert db.execute('SELECT count(*) FROM oxygen_records').fetchone()[0]==len(rows)
        assert db.execute('SELECT count(*) FROM respiration_windows').fetchone()[0]>0
    assert hrv_analysis.run_once(end+40)[0]==0


def test_api_requires_auth_and_empty_means_unknown(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('INGEST_TOKEN','a'*32)
    with TestClient(app) as c:
        assert c.get('/v1/hrv').status_code==401
        result=c.get('/v1/hrv',headers={'Authorization':'Bearer '+'a'*32}).json()
        assert result['rmssd_ms'] is None and result['state']=='awaiting_history'


def test_last_qualified_hrv_survives_morning_failures_with_original_time(db):
    end=1800000300;now=end+3*3600
    db.execute("INSERT INTO settings VALUES('device','test')")
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(now),))
    temperature.record_firmware(firmware(receivedAt=end-360),db)
    for row in series(end):hrv.index(db,row)
    hrv.compute_window(db,'test',end)
    hrv.compute_window(db,'test',now)  # No current intervals does not erase history.
    result=hrv.status(db,now);metrics=hrv.metrics(db,now)
    assert result['state']=='stale' and result['end']==end
    assert metrics['whoop_hrv_last_qualified_rmssd_ms']==20
    assert metrics['whoop_hrv_last_qualified_end_timestamp_seconds']==end
    assert metrics['whoop_hrv_ready']==0
    assert 'whoop_hrv_rmssd_ms' not in metrics  # Live metric retains its contract.
    db.execute("UPDATE settings SET value=? WHERE key='hrv_heartbeat'",(str(end+86401),))
    assert 'whoop_hrv_last_qualified_rmssd_ms' not in hrv.metrics(db,end+86401)
    assert 'whoop_hrv_last_qualified_rmssd_ms' not in hrv.metrics(db,now+181)


def test_repeated_firmware_response_does_not_restart_all_analysis(tmp_path,monkeypatch):
    import vitals
    monkeypatch.setenv('DATA_DIR',str(tmp_path));storage.initialize()
    now=1800000300
    invalidations=[]
    monkeypatch.setattr(vitals,'invalidate_firmware',lambda db,at:invalidations.append(at))
    with storage.connect() as db:
        temperature.record_firmware(firmware(receivedAt=now-360),db)
    hrv_analysis.run_once(now)
    assert invalidations==[now]
    with storage.connect() as db:
        temperature.record_firmware(firmware(receivedAt=now+1),db)
    hrv_analysis.run_once(now+2)
    assert invalidations==[now], 'A reconnect reporting the same version changes no firmware eligibility'
    with storage.connect() as db:
        db.execute("INSERT INTO firmware_observations VALUES('test',?,'unknown','changed')",(now+3,))
    hrv_analysis.run_once(now+4)
    assert invalidations==[now,now+4]
    # Late evidence moving the first verified boundary must still revisit old windows.
    with storage.connect() as db:
        temperature.record_firmware(firmware(receivedAt=now-600),db)
    hrv_analysis.run_once(now+5)
    assert invalidations==[now,now+4,now+5]


def test_analysis_backlog_drains_without_idle_sleep(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));storage.initialize();now=1800000300
    assert hrv_analysis.next_delay(0,now)==30
    assert hrv_analysis.next_delay(2000,now)==0
    with storage.connect() as db:
        db.execute("INSERT INTO respiration_dirty VALUES('test',?,1)",(now-60,))
    assert hrv_analysis.next_delay(0,now)==1
    with storage.connect() as db:
        db.execute('DELETE FROM respiration_dirty')
        db.execute("INSERT INTO hrv_dirty VALUES('test',?)",(now+60,))
    assert hrv_analysis.next_delay(0,now)==30


def test_backfill_status_uses_downloaded_window_not_unfilled_tail(db):
    end=1800000300;now=end+3*3600
    db.execute("INSERT INTO settings VALUES('device','test')")
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(now),))
    temperature.record_firmware(firmware(receivedAt=end-360),db)
    for row in series(end):hrv.index(db,row)
    hrv.compute_window(db,'test',end)
    hrv.compute_window(db,'test',end+240)
    result=hrv.status(db,now)
    assert result['latest_attempt']['end']==end
    assert result['history_end']<end and result['rmssd_ms']==20
