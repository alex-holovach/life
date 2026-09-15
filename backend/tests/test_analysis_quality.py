import json
import pytest
import storage
import hrv
import respiration
import temperature
from test_temperature import firmware
import analysis_quality


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path)); storage.initialize()
    with storage.connect() as db:
        db.execute("INSERT INTO settings VALUES('device','test')")
        temperature.record_firmware(firmware(receivedAt=1_799_900_000), db)
        yield db


def window(db, end, reasons=(), value=25):
    result = dict(start=end-300, end=end, state='insufficient_data' if reasons else 'ready',
                  rmssd_ms=None if reasons else value, reasons=list(reasons),coverage=1,pairs=300)
    db.execute('INSERT INTO hrv_windows VALUES(?,?,?,?)',
               ('test', end, hrv.ALGORITHM, json.dumps(result)))


def test_overlapping_windows_count_covered_time_once_and_exclude_backfill_tail(db):
    now=1_800_000_000; through=now-3600
    hrv.index(db, dict(device='test',at=through-.5,seq=10,words=[950],hr=63))
    window(db, through-60); window(db, through)
    window(db, through+60, ['missing_history_records'])
    window(db, through-120, ['too_few_adjacent_pairs'])
    report=analysis_quality.calculate(db, now)
    assert report['hrv']['analyzed_windows']==3
    assert report['hrv']['qualified_windows']==2
    assert report['hrv']['qualified_seconds']==360
    assert report['hrv']['reasons']=={'too_few_adjacent_pairs':1}
    assert [p['at'] for p in report['hrv']['points']]==[through-60,through]
    assert report['history']['through']==through-.5


def test_collection_coverage_does_not_claim_missing_or_conflicting_time(db):
    now=1_800_000_000
    for at,seq,words,conflict in [(now-10,1,[950],0),(now-9,2,[],0),
                                  (now-6,5,[950],0),(now-5,6,[950],1),
                                  (now-4,7,[950],0),(now-3,8,[950],0)]:
        hrv.index(db,dict(device='test',at=at,seq=seq,words=words,hr=63))
        if conflict:db.execute('UPDATE hrv_records SET conflict=1 WHERE at=?',(at,))
    history=analysis_quality.calculate(db,now)['history']
    assert history['recorded_seconds']==2
    assert history['empty_interval_records']==1
    assert history['conflicting_records']==1
    assert history['record_coverage']==pytest.approx(2/86400)
    assert history['through']==now-3


def test_missing_data_remains_distinct_from_no_qualifying_signal(db):
    report=analysis_quality.calculate(db,1_800_000_000)
    assert report['history']['through'] is None
    assert report['hrv']['analyzed_windows']==0
    assert report['respiration']['qualified_seconds']==0
    assert report['hrv']['points']==[]


def test_cached_report_expires_and_unknown_firmware_suppresses_it(db):
    now=1_800_000_000
    db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(now),))
    analysis_quality.save(db,now)
    assert analysis_quality.status(db,now)['state']=='ready'
    assert analysis_quality.status(db,now+181)['state']=='analysis_unavailable'
    db.execute('INSERT INTO firmware_observations VALUES(?,?,?,?)',('test',now+1,'unknown','new'))
    assert analysis_quality.status(db,now+1)['state']=='unsupported_firmware'


def test_quality_endpoint_and_metrics_require_active_analysis(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    now=1_800_000_000
    monkeypatch.setenv('DATA_DIR',str(tmp_path)); monkeypatch.setenv('INGEST_TOKEN','a'*32)
    monkeypatch.setattr('app.time.time',lambda:now)
    with TestClient(app) as client:
        assert client.get('/v1/hrv').status_code==401
        with storage.connect() as db:
            db.execute("INSERT INTO settings VALUES('device','test')")
            temperature.record_firmware(firmware(receivedAt=now-1000),db)
            db.execute("INSERT INTO settings VALUES('hrv_heartbeat',?)",(str(now),))
            hrv.index(db,dict(device='test',at=now-1,seq=10,words=[950],hr=63))
            window(db,now-60)
            analysis_quality.save(db,now)
        response=client.get('/v1/hrv',headers={'Authorization':'Bearer '+'a'*32})
        assert response.json()['quality_report']['hrv']['qualified_windows']==1
        metrics=client.get('/metrics').text
        assert 'whoop_hrv_qualified_windows_24h{device="strap",algorithm="r24_rmssd_5m_v1"} 1' in metrics
        with storage.connect() as db:db.execute("DELETE FROM settings WHERE key='hrv_heartbeat'")
        assert 'whoop_hrv_qualified_windows_24h{' not in client.get('/metrics').text
