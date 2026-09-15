import gzip
import json
import os
import time
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from app import app
import storage
from worker import drain_once

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INGEST_TOKEN", "synthetic-test-token-" + "x" * 32)
    monkeypatch.setenv("BACKLOG_DAYS", "30")
    storage.initialize()

@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as client: yield client

def capture(**fields):
    return dict(id=str(uuid.uuid4()), deviceId="fixture-strap", sessionId="test-session",
                receivedAt=time.time(), source="battery_verified", rawBase64="FA==",
                batteryPercent=20.5, quality="cross_checked", decoderVersion="test") | fields

def headers(): return {"Authorization": "Bearer " + os.environ["INGEST_TOKEN"]}

def post(client, *rows): return client.post("/v1/batches", json={"captures": rows}, headers=headers())

def rows():
    with storage.connect() as db: return [dict(row) for row in db.execute("SELECT * FROM samples ORDER BY timestamp")]

def test_auth_validation_and_idempotent_commit(client):
    row = capture()
    assert client.post("/v1/batches", json={"captures": [row]}).status_code == 401
    assert client.get("/v1/status").status_code == 401
    for _ in range(2): assert post(client, row).status_code == 204
    assert storage.status()["captured_records"] == 1
    assert len(rows()) == 1
    archive, = list(storage.directory().glob("archive/*/*.gz"))
    assert json.loads(gzip.decompress(archive.read_bytes()))["captures"][0] == row
    assert archive.stat().st_mode & 0o777 == 0o600
    assert post(client, capture(batteryPercent=101)).status_code == 422
    assert post(client, capture(rawBase64="invalid!")).status_code == 422
    assert post(client, capture(intervalWords=[65536])).status_code == 422
    assert post(client, capture(sampleAt=time.time()+1000)).status_code == 422
    assert client.post("/v1/batches", content=b" " * (2*1024*1024+1), headers=headers()).status_code == 413
    assert storage.status()["captured_records"] == 1

def test_sensor_inventory_replay_late_arrival_auth_and_no_unvalidated_metrics(client):
    import base64
    from test_protocol import LIVE
    now = time.time()
    row = capture(source="wire_frame", batteryPercent=None, quality="crc_valid",
                  receivedAt=now, rawBase64=base64.b64encode(LIVE).decode())
    for _ in range(2):
        assert post(client, row).status_code == 204
    assert post(client, row | {'id': 'older', 'receivedAt': now-5}).status_code == 204
    assert client.get('/v1/sensors').status_code == 401
    result = client.get('/v1/sensors', headers=headers()).json()['sensors']
    assert len(result) == 1
    assert result[0]['frames'] == 2
    assert result[0]['received_at'] == now
    assert result[0]['decoded']['time_status'] == 'non_unix_device_clock'
    assert not rows()


def test_storage_failure_never_acknowledges(client, monkeypatch):
    def fail(*args): raise OSError("simulated full disk")
    monkeypatch.setattr(storage.os, "fsync", fail)
    assert post(client, capture()).status_code == 500
    assert storage.status()["captured_records"] == 0

def test_conflicting_id_rolls_back_entire_batch(client):
    row = capture()
    assert post(client, row).status_code == 204
    assert post(client, capture(), row | {"batteryPercent": 1}).status_code == 409
    assert storage.status()["captured_records"] == 1

def test_history_replay_ignores_receipt_session_but_not_raw(client):
    row = capture(source="history", sampleAt=time.time()-1000, quality="historical_intervals_unverified", hrBpm=65)
    assert post(client, row).status_code == 204
    assert post(client, row | {"receivedAt": time.time(), "sessionId": "reconnected"}).status_code == 204
    assert storage.status()["captured_records"] == 1
    assert post(client, row | {"rawBase64": "AQ=="}).status_code == 409

def test_other_strap_cannot_mix_into_fixed_series(client):
    assert post(client, capture()).status_code == 204
    assert post(client, capture(deviceId="another-strap")).status_code == 409

def test_stale_and_out_of_order_battery_do_not_look_fresh(client):
    now=time.time()
    fresh=capture(receivedAt=now, sampleAt=now, batteryPercent=9.6)
    stale=capture(receivedAt=now-7200, sampleAt=now-7200, batteryPercent=80)
    assert post(client, fresh, stale, capture(source="battery", batteryPercent=100, quality="unverified_standard_battery")).status_code == 204
    status=storage.status()
    assert status["battery_percent"] == 9.6 and status["battery_observed_at"] == now
    assert [r["value"] for r in rows()] == [80, 9.6]
    assert rows()[0]["timestamp"] == int((now-7200)*1000)

def test_local_diagnostics_do_not_make_strap_traffic_look_fresh(client):
    now = time.time()
    assert post(client, capture(source="live_hr", receivedAt=now-120, hrBpm=70, quality="live_receipt_timestamp")) .status_code == 204
    assert post(client, capture(source="collector_event", receivedAt=now), capture(source="research_command", receivedAt=now)) .status_code == 204
    assert storage.status()["last_received"] == now-120

def test_unverified_intervals_zero_hr_off_wrist_never_exported(client):
    assert post(client, capture(source="live_hr", hrBpm=0, quality="sensor_acquiring", rrMs=[763.67], intervalWords=[782]),
                capture(source="live_hr", hrBpm=65, quality="off_wrist"),
                capture(source="battery", quality="unverified_standard_battery")) .status_code == 204
    assert rows() == []
    assert storage.status()["captured_records"] == 3

def test_conflicting_sample_retained_without_overwrite(client):
    now=time.time()
    assert post(client, capture(sampleAt=now, batteryPercent=20), capture(sampleAt=now, batteryPercent=21)).status_code == 204
    assert len(rows()) == 1 and rows()[0]["value"] == 20
    assert storage.status()["conflicts"] == 1

def test_old_data_held_for_import_not_retimestamped(client):
    at=time.time()-31*86400
    assert post(client, capture(receivedAt=at)).status_code == 204
    row,=rows()
    assert row["timestamp"] == int(at*1000) and row["state"] == "historical_import"

def test_prometheus_retry_backoff_recovery_and_permanent_failure(client):
    now=time.time()
    assert post(client, capture(receivedAt=now-120)).status_code == 204
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(503 if len(requests)==1 else 204)
    with httpx.Client(transport=httpx.MockTransport(respond)) as remote:
        assert drain_once(remote, now)
        assert rows()[0]["state"] == "pending" and rows()[0]["attempts"] == 1
        assert not drain_once(remote, now+1)
        assert len(requests)==1
        assert drain_once(remote, now+60)
    assert rows()[0]["state"] == "sent"
    assert requests[0].content == requests[1].content
    assert requests[0].headers["content-encoding"] == "snappy"
    assert post(client, capture()).status_code == 204
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400))) as remote:
        assert drain_once(remote)
        assert not drain_once(remote)
    assert storage.status()["samples"]["rejected"] == 1

def test_connection_failure_and_429_preserve_queue(client):
    assert post(client, capture()).status_code == 204
    def fail(request): raise httpx.ConnectError("offline", request=request)
    for transport in [httpx.MockTransport(fail), httpx.MockTransport(lambda request: httpx.Response(429))]:
        with storage.connect() as db: db.execute("UPDATE samples SET next_attempt=0")
        with httpx.Client(transport=transport) as remote: assert drain_once(remote)
        assert rows()[0]["state"] == "pending"

def test_real_prometheus_timestamped_backfill_and_replay(client):
    url=os.environ.get("PROMETHEUS_TEST_URL")
    if not url: pytest.skip("Run scripts/check_prometheus.py for a real receiver")
    now=int(time.time())
    with httpx.Client(timeout=10) as remote:
        for timestamp,value in [(now-300,71), (now-600,65)]:
            row=capture(receivedAt=timestamp, source="live_hr", quality="live_receipt_timestamp", hrBpm=value)
            assert post(client,row).status_code==204
            assert drain_once(remote)
        # Simulate a process crash after remote acceptance but before its local acknowledgement.
        with storage.connect() as db: db.execute("UPDATE samples SET state='pending'")
        assert drain_once(remote)
        result=remote.get(url+"/api/v1/query",params={"query":'whoop_heart_rate_bpm{device="strap",source="live"}[1h]',"time":now}).json()
    assert result["status"]=="success"
    assert result["data"]["result"][0]["values"] == [[now-600,"65"],[now-300,"71"]]
    assert storage.status()["samples"]=={"sent":2}


def test_temperature_api_and_archive_replay_do_not_invent_degrees(client):
    from test_temperature import capture as temperature_capture
    from temperature_reprocess import reprocess
    c = temperature_capture()
    row = capture(source='history', rawBase64=c.rawBase64, batteryPercent=None, quality='historical_intervals_unverified')
    assert client.get('/v1/temperature').status_code == 401
    assert post(client,row).status_code == 204
    state=client.get('/v1/temperature',headers=headers()).json()
    assert state['state']=='awaiting_firmware' and state['celsius'] is None
    assert len(state['candidates'])==7
    assert reprocess()['inserted_samples']==0
    # Simulate adding a decoder to a preexisting archive, without rewriting captures.
    with storage.connect() as db:
        db.execute("DELETE FROM samples WHERE series LIKE '%whoop_temperature_candidate_raw%'")
    assert reprocess()['inserted_samples']==7
    assert reprocess()['inserted_samples']==0


def test_temperature_firmware_replay_api_sensor_error_and_staleness(client):
    import base64
    import struct
    import temperature
    from test_temperature import capture as tc, firmware
    from test_protocol import seal
    from temperature_reprocess import reprocess
    c=tc();f=firmware(deviceId='fixture-strap')
    row=capture(source='history',rawBase64=c.rawBase64,batteryPercent=None)
    # Historical data can arrive before the version response; replay fills it later.
    assert post(client,row).status_code==204
    assert post(client,capture(source='wire_frame',rawBase64=f.rawBase64,receivedAt=f.receivedAt,batteryPercent=None)).status_code==204
    assert reprocess()['inserted_samples']==2
    assert reprocess()['inserted_samples']==0
    data=client.get('/v1/temperature',headers=headers()).json()
    assert data['state']=='ready' and data['celsius']==33.0
    assert data['decoder']==temperature.DECODER
    # Re-index firmware solely from immutable archived responses, without private config.
    with storage.connect() as db:
        db.execute('DELETE FROM firmware_observations')
    assert reprocess()['inserted_samples']==0
    raw=bytearray(base64.b64decode(c.rawBase64))
    struct.pack_into('<I',raw,11,int(time.time())-10)
    struct.pack_into('<H',raw,76,700)
    assert post(client,capture(source='history',rawBase64=base64.b64encode(seal(raw)).decode(),batteryPercent=None)).status_code==204
    data=client.get('/v1/temperature',headers=headers()).json()
    assert data['state']=='sensor_unavailable' and data['celsius'] is None
    with storage.connect() as db:
        status=temperature.status(db,storage.directory(),time.time()+900)
        valid=db.execute('SELECT value FROM samples WHERE series=? ORDER BY timestamp DESC LIMIT 1',
                         (temperature.metric(temperature.VALID_METRIC,decoder=temperature.DECODER),)).fetchone()[0]
    assert status['state']=='stale' and valid==0
    assert reprocess()['inserted_samples']==0


def test_temperature_unsupported_firmware_hides_old_current_value(client):
    import base64
    import struct
    from test_temperature import firmware, capture as tc, VERSION
    from test_protocol import seal
    f=firmware();c=tc()
    assert post(client,capture(source='wire_frame',rawBase64=f.rawBase64,receivedAt=f.receivedAt,batteryPercent=None),
                capture(source='history',rawBase64=c.rawBase64,batteryPercent=None)).status_code==204
    assert client.get('/v1/temperature',headers=headers()).json()['state']=='ready'
    raw=bytearray(VERSION);struct.pack_into('<I',raw,10,42)
    assert post(client,capture(source='wire_frame',rawBase64=base64.b64encode(seal(raw)).decode(),batteryPercent=None)).status_code==204
    data=client.get('/v1/temperature',headers=headers()).json()
    assert data['state']=='unsupported_firmware' and data['celsius'] is None
    assert 'whoop_skin_temperature_decoder_supported{device="strap"} 0' in client.get('/metrics').text
