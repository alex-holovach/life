import base64
import json
import math
import random
import struct
import time
from types import SimpleNamespace
import pytest
import hrv
import respiration
import storage
import temperature
import vitals
from test_protocol import seal
from test_temperature import HISTORY, firmware


def breathing_rows(end, rate=12, amplitude=30, duration=300):
    at=end-duration;start=at;beat=at;rows=[];seq=1
    while at<end:
        words=[]
        while beat<=at:
            word=round(800+amplitude*math.sin(2*math.pi*rate/60*(beat-start)))
            words.append(word);beat+=word/1000
        rows.append(dict(device='test',at=at,seq=seq,words=words,hr=75,conflict=0))
        seq+=1;at+=100/104
    return rows


@pytest.mark.parametrize('rate',[8,12,20])
def test_known_sinusoidal_modulation(rate):
    result=respiration.calculate(breathing_rows(1800000300,rate),1800000300)
    assert result['state']=='ready',result
    assert abs(result['breaths_per_minute']-rate)<.3
    assert result['validation']=='not_reference_validated'


def test_flat_noisy_and_changing_frequency_are_not_breathing_measurements():
    end=1800000300
    assert respiration.calculate(breathing_rows(end,amplitude=0),end)['breaths_per_minute'] is None
    rows=breathing_rows(end);random.seed(8)
    for row in rows:row['words']=[random.randint(770,830) for _ in row['words']]
    assert respiration.calculate(rows,end)['breaths_per_minute'] is None
    rows=breathing_rows(end)
    for row in rows[len(rows)//2:]:
        row['words']=[round(800+30*math.sin(2*math.pi*22/60*(row['at']-(end-300)))) for _ in row['words']]
    assert respiration.calculate(rows,end)['breaths_per_minute'] is None


@pytest.mark.parametrize('damage',['gap','empty','clamp','conflict','sequence','artifact'])
def test_missing_and_corrupt_intervals_cannot_create_a_contiguous_window(damage):
    end=1800000300;rows=breathing_rows(end)
    if damage=='gap':rows=[r for i,r in enumerate(rows) if i%50!=0]
    else:
        for row in rows[::50]:
            if damage=='empty':row['words']=[]
            if damage=='clamp':row['words']=[500]
            if damage=='artifact':row['words']=[1900]
            if damage=='conflict':row['conflict']=1
            if damage=='sequence':row['seq']+=30
    assert respiration.calculate(rows,end)['breaths_per_minute'] is None


def test_short_recording_is_not_enough_even_with_a_clear_signal():
    assert respiration.calculate(breathing_rows(1800000300,duration=60),1800000300)['breaths_per_minute'] is None


def test_backfill_diagnostic_does_not_select_an_unfilled_future_window(db):
    end=1800000300;rows=breathing_rows(end)
    for row in rows:hrv.index(db,row)
    full=respiration.calculate(rows,end);tail=respiration.calculate(rows,end+240)
    for at,result in [(end,full),(end+240,tail)]:
        db.execute('INSERT INTO respiration_windows VALUES(?,?,?,?)',('test',at,respiration.ALGORITHM,json.dumps(result)))
    result=vitals.respiration_status(db,'test',end+3600)
    assert result['latest_attempt']['analyzed_until']==end
    assert result['history_end']==rows[-1]['at']


@pytest.mark.parametrize('code,expected,state',[
    (0,None,'awaiting_device_reading'),(98,None,'ambiguous_firmware_value'),
    (1,None,'device_signal_unavailable'),(2,None,'device_signal_unavailable'),
    (3,None,'device_signal_unavailable'),(60,None,'device_signal_unavailable'),
    (128,None,'device_signal_unavailable'),(255,None,'device_signal_unavailable'),
    (70,70,'ready'),(95,95,'ready'),(100,100,'ready')])
def test_oxygen_codes_and_ambiguous_98(code,expected,state):
    assert vitals.oxygen_value(code)==(expected,state)


def test_oxygen_requires_valid_crc_frame_and_timestamp():
    raw=bytearray(HISTORY);raw[86]=95
    c=SimpleNamespace(source='history',deviceId='test',receivedAt=1900000000,
                      rawBase64=base64.b64encode(seal(raw)))
    assert vitals.oxygen_record(c)['code']==95
    raw[86]^=1;c.rawBase64=base64.b64encode(raw)
    assert vitals.oxygen_record(c) is None
    struct.pack_into('<H',raw,15,32768);c.rawBase64=base64.b64encode(seal(raw))
    assert vitals.oxygen_record(c) is None


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));storage.initialize()
    with storage.connect() as db:yield db


def test_oxygen_firmware_conflicts_freshness_and_zero_do_not_invent_a_value(db):
    end=1800000300
    db.execute("INSERT INTO settings VALUES('device','test')")
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(end),))
    vitals.index(db,dict(device='test',at=end-10,code=95),None)
    assert vitals.status(db,end)['oxygen']['percent'] is None
    temperature.record_firmware(firmware(receivedAt=end-300),db)
    assert vitals.status(db,end)['oxygen']['percent']==95
    vitals.index(db,dict(device='test',at=end-5,code=0),None)
    result=vitals.status(db,end)['oxygen']
    assert result['percent']==95 and result['observed_at']==end-10
    assert result['latest_attempt']['raw_code']==0
    vitals.index(db,dict(device='test',at=end-10,code=98),None)
    assert vitals.status(db,end)['oxygen']['percent'] is None
    assert 'whoop_spo2_percent' not in vitals.metrics(db,end)
    vitals.index(db,dict(device='test',at=end-2,code=97),None)
    assert vitals.status(db,end)['oxygen']['percent']==97
    assert vitals.status(db,end+86401)['oxygen']['percent'] is None


def test_respiration_uses_segment_age_not_analysis_time_and_suppresses_failed_worker(db):
    now=1800000300
    db.execute("INSERT INTO settings VALUES('device','test')")
    temperature.record_firmware(firmware(receivedAt=now-2000),db)
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(now),))
    result=respiration.calculate(breathing_rows(now),now)
    db.execute('INSERT INTO respiration_windows VALUES(?,?,?,?)',('test',now,respiration.ALGORITHM,json.dumps(result)))
    assert vitals.status(db,now)['respiration']['breaths_per_minute']==12
    assert vitals.status(db,now+181)['respiration']['breaths_per_minute'] is None
    db.execute("UPDATE settings SET value=? WHERE key='hrv_heartbeat'",(str(now+1000),))
    assert vitals.status(db,now+1000)['respiration']['state']=='stale'
    assert 'whoop_respiratory_rate_estimated_breaths_per_minute' not in vitals.metrics(db,now+1000)


def test_scheduled_analysis_firmware_gate_and_recompute(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));storage.initialize();end=1800000300
    with storage.connect() as db:
        for row in breathing_rows(end):hrv.index(db,row)
        db.execute('INSERT INTO respiration_dirty VALUES(?,?,1)',('test',end))
    assert vitals.analyze(storage.connect,end+20)==1
    with storage.connect() as db:
        assert json.loads(db.execute('SELECT result FROM respiration_windows').fetchone()[0])['state']=='unsupported_firmware'
        temperature.record_firmware(firmware(receivedAt=end-360),db)
        vitals.invalidate_firmware(db,end+20)
    assert vitals.analyze(storage.connect,end+20)==1
    with storage.connect() as db:
        assert json.loads(db.execute('SELECT result FROM respiration_windows').fetchone()[0])['breaths_per_minute']==12


def test_vitals_endpoint_requires_auth_and_unknown_is_not_zero(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('INGEST_TOKEN','a'*32)
    with TestClient(app) as c:
        assert c.get('/v1/vitals').status_code==401
        r=c.get('/v1/vitals',headers={'Authorization':'Bearer '+'a'*32})
        assert r.status_code==200
        assert r.json()['oxygen']['percent'] is None
        assert r.json()['respiration']['breaths_per_minute'] is None


def test_last_qualified_breathing_survives_failed_new_windows(db):
    end=1800000300;now=end+3*3600
    db.execute("INSERT INTO settings VALUES('device','test')")
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(now),))
    temperature.record_firmware(firmware(receivedAt=end-360),db)
    valid=respiration.calculate(breathing_rows(end),end)
    failed=respiration.calculate([],now)
    for at,result in [(end,valid),(now,failed)]:
        db.execute('INSERT INTO respiration_windows VALUES(?,?,?,?)',('test',at,respiration.ALGORITHM,json.dumps(result)))
    result=vitals.status(db,now)['respiration'];metrics=vitals.metrics(db,now)
    assert result['state']=='stale' and result['breaths_per_minute']==12
    assert result['end']==valid['end'] and result['latest_attempt']['reasons']==['no_contiguous_intervals']
    assert metrics['whoop_respiratory_rate_last_qualified_breaths_per_minute']==12
    assert metrics['whoop_respiratory_rate_ready']==0
    assert 'whoop_respiratory_rate_estimated_breaths_per_minute' not in metrics
    db.execute("UPDATE settings SET value=? WHERE key='hrv_heartbeat'",(str(end+86401),))
    assert 'whoop_respiratory_rate_last_qualified_breaths_per_minute' not in vitals.metrics(db,end+86401)


def test_breathing_failure_reports_signal_reason_in_metrics(db):
    end=1800000300
    db.execute("INSERT INTO settings VALUES('device','test')")
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(end),))
    temperature.record_firmware(firmware(receivedAt=end-360),db)
    failed=respiration.calculate(breathing_rows(end,duration=30),end)
    db.execute('INSERT INTO respiration_windows VALUES(?,?,?,?)',('test',end,respiration.ALGORITHM,json.dumps(failed)))
    assert vitals.metrics(db,end)['whoop_respiratory_rate_signal_status']==3
