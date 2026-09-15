"""Synthetic scenarios only. No personal telemetry or production defaults."""
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from app import app
import storage
import wellness

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('INGEST_TOKEN','synthetic-test-token-'+'x'*32)
    storage.initialize()

AUTH={'Authorization':'Bearer synthetic-test-token-'+'x'*32}
def records(start,n,hr=150):
    return [dict(at=start+i,seq=i,hr=hr,conflict=0) for i in range(n)]

def test_edwards_zones_known_load_and_gaps():
    # Five 2-minute zones at 55/65/75/85/95% maximum produce 30 AU.
    rows=records(0,601)
    for r in rows:r['hr']=[110,130,150,170,190][min(4,int(r['at']//120))]
    result=wellness.strain(rows,0,86400,600,200)
    assert result['load']==pytest.approx(30)
    assert result['zone_minutes']==pytest.approx([2]*5)
    assert result['coverage']==1 and 0<result['score']<21
    gaps=wellness.strain(rows[:300]+rows[400:],0,86400,600,200)
    assert gaps['recorded_seconds']==499 and gaps['score'] is None
    assert wellness.strain(rows,0,86400,600,None)['score'] is None
    rows[300]['conflict']=1
    assert wellness.strain(rows,0,86400,600,200)['recorded_seconds']==598

def test_daily_ceiling_and_dst():
    result=wellness.strain(records(0,86401,200),0,86400,86400,200)
    assert result['score']==pytest.approx(21)
    for month,day,hours in [(3,8,23),(11,1,25)]:
        now=datetime(2026,month,day,12,tzinfo=ZoneInfo('America/Los_Angeles')).timestamp()
        a,b,label=wellness.day_bounds(now,'America/Los_Angeles')
        assert b-a==hours*3600

def test_sleep_auth_validation_idempotency_revision_overlap():
    with TestClient(app) as client:
        now=time.time();identifier=str(uuid.uuid4());route='/v1/sleep/'+identifier
        value=dict(revision=1,start=now-8*3600,end=now-3600,awake_seconds=1800)
        assert client.put(route,json=value).status_code==401
        assert client.get('/v1/wellness').status_code==401
        for _ in range(2):assert client.put(route,json=value,headers=AUTH).status_code==200
        with storage.connect() as db:assert db.execute('SELECT count(*) FROM wellness_edits').fetchone()[0]==1
        assert client.put(route,json=value|dict(end=now-3000),headers=AUTH).status_code==409
        assert client.put('/v1/sleep/'+str(uuid.uuid4()),json=value,headers=AUTH).status_code==409
        assert client.put(route,json=value|dict(revision=2,end=now-3000),headers=AUTH).status_code==200
        assert client.put(route,json=value,headers=AUTH).status_code==409
        assert client.put(route,json=value|dict(revision=3,end=now+100),headers=AUTH).status_code==422
        assert client.put('/v1/profile',json=dict(timezone='invalid'),headers=AUTH).status_code==422
        assert client.put('/v1/profile',json=dict(timezone='UTC',max_hr=60),headers=AUTH).status_code==422

def test_real_sleep_score_no_defaults_and_cache_invalidation():
    now=time.time();identifier=str(uuid.uuid4())
    with storage.connect() as db:
        wellness.save_sleep(db,identifier,dict(revision=1,start=now-8*3600,end=now,awake_seconds=1800),now)
    wellness.analyze(storage.connect,now)
    with storage.connect() as db:
        result=wellness.status(db,now)
        assert result['sleep']['duration_seconds']==7.5*3600
        assert result['sleep']['score'] is None
        assert result['sleep']['heart_rate_bpm'] is None
        assert result['sleep']['temperature_celsius'] is None
        assert 'life_strain_coverage_ratio' not in wellness.metrics(db,now)
        wellness.save_profile(db,dict(timezone='UTC',max_hr=None,sleep_goal_minutes=480),now)
    with storage.connect() as db:assert wellness.status(db,now)['sleep'] is None
    wellness.analyze(storage.connect,now)
    with storage.connect() as db:
        result=wellness.status(db,now)
        assert result['sleep']['score']==pytest.approx(93.75)
        assert 'life_strain_score' not in wellness.metrics(db,now)
        assert wellness.metrics(db,now)['life_sleep_score']==93.75
        assert wellness.status(db,now+181)['sleep'] is None
    wellness.analyze(storage.connect,now+37*3600)
    with storage.connect() as db:assert 'life_sleep_score' not in wellness.metrics(db,now+37*3600)

def test_sleep_hr_and_hrv_require_coverage():
    now=time.time();start=now-1800
    with storage.connect() as db:
        db.execute("INSERT INTO settings VALUES('device','fixture')")
        db.execute('INSERT INTO firmware_observations VALUES(?,?,?,?)',('fixture',start-1,wellness.temperature.FIRMWARE,'fixture'))
        for r in records(start,1801,60):
            db.execute('INSERT INTO hrv_records VALUES(?,?,?,?,?,?)',('fixture',r['at'],r['seq'],'[]',r['hr'],0))
        session=dict(id='fixture',start=start,end=now,awake_seconds=0)
        result=wellness.sleep_result(db,session,'fixture',None)
        assert result['heart_rate_bpm']==60
        assert result['hrv_ms'] is None
        for end in [start+300,start+600,start+900]:
            value=dict(state='ready',start=end-300,end=end,rmssd_ms=40)
            db.execute('INSERT INTO hrv_windows VALUES(?,?,?,?)',('fixture',end,'r24_rmssd_5m_v1',json.dumps(value)))
        result=wellness.sleep_result(db,session,'fixture',None)
        assert result['hrv_ms']==40 and result['hrv_coverage']==.5
        db.execute('DELETE FROM hrv_records WHERE at<?',(start+600,))
        assert wellness.sleep_result(db,session,'fixture',None)['heart_rate_bpm'] is None

def test_overnight_baseline_requires_seven_distinct_prior_nights(monkeypatch):
    now=datetime(2026,9,14,12,tzinfo=ZoneInfo('UTC')).timestamp()
    with storage.connect() as db:
        wellness.save_profile(db,dict(timezone='UTC',max_hr=None,sleep_goal_minutes=None),now)
        for index in range(8):
            end=now-(7-index)*86400-3600
            # One short nap must not enter the reference baseline.
            db.execute('INSERT INTO sleep_sessions VALUES(?,?,?,?,?,?)',(f'night-{index}',1,end-8*3600,end,0,now))
        db.execute('INSERT INTO sleep_sessions VALUES(?,?,?,?,?,?)',('nap',1,now-2*86400+60,now-2*86400+3660,0,now))
    def summary(db,session,device,goal):
        duration=session['end']-session['start']
        return dict(id=session['id'],start=session['start'],end=session['end'],duration_seconds=duration,
                    temperature_celsius=50 if session['id']=='nap' else (34 if session['id']=='night-7' else 33),
                    temperature_delta_celsius=None,baseline_nights=0)
    monkeypatch.setattr(wellness,'sleep_result',summary)
    wellness.analyze(storage.connect,now)
    with storage.connect() as db:
        current=json.loads(db.execute("SELECT result FROM wellness_results WHERE key='sleep:night-7'").fetchone()[0])
        assert current['baseline_nights']==7 and current['temperature_delta_celsius']==1
        earlier=json.loads(db.execute("SELECT result FROM wellness_results WHERE key='sleep:night-6'").fetchone()[0])
        assert earlier['temperature_delta_celsius'] is None

def test_unknown_firmware_cannot_supply_strain_intervals():
    with storage.connect() as db:
        for r in records(1800000000,1000):
            db.execute('INSERT INTO hrv_records VALUES(?,?,?,?,?,?)',('fixture',r['at'],r['seq'],'[]',r['hr'],0))
        assert not wellness.history_rows(db,'fixture',1800000000,1800001000)

def test_past_sleep_entries_are_listed_and_editable_without_changing_latest_sleep():
    with TestClient(app) as client:
        now=time.time();recent=str(uuid.uuid4());past=str(uuid.uuid4())
        latest=dict(revision=1,start=now-8*3600,end=now-3600,awake_seconds=0)
        older=dict(revision=1,start=now-14*86400-8*3600,end=now-14*86400,awake_seconds=1800)
        assert client.put('/v1/sleep/'+recent,json=latest,headers=AUTH).status_code==200
        assert client.put('/v1/sleep/'+past,json=older,headers=AUTH).status_code==200
        corrected=older|dict(revision=2,awake_seconds=3600)
        for _ in range(2):assert client.put('/v1/sleep/'+past,json=corrected,headers=AUTH).status_code==200
        wellness.analyze(storage.connect,now)
        result=client.get('/v1/wellness',headers=AUTH).json()
        assert [s['id'] for s in result['sessions']]==[recent,past]
        assert result['sessions'][1]['revision']==2
        assert result['sessions'][1]['awake_seconds']==3600
        assert result['sleep']['id']==recent
        with storage.connect() as db:
            historical=json.loads(db.execute('SELECT result FROM wellness_results WHERE key=?',('sleep:'+past,)).fetchone()[0])
            assert historical['duration_seconds']==7*3600
