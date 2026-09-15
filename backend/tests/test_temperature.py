import base64
import json
import struct
import time
from types import SimpleNamespace
import pytest
import storage
import temperature
from test_protocol import frame, seal

# Synthetic, CRC-sealed frames with explicitly assigned fields. No personal capture.
_history=frame(47,24,104)
struct.pack_into('<I',_history,7,1)
struct.pack_into('<I',_history,11,1800000000)
struct.pack_into('<H',_history,15,16384)
_history[21]=60;_history[22]=1;struct.pack_into('<H',_history,23,1000)
struct.pack_into('<H',_history,72,1000);struct.pack_into('<H',_history,76,330)
HISTORY=seal(_history)
_version=frame(36,1,84);_version[6]=7;_version[8:10]=b'\x01\x01'
struct.pack_into('<4I',_version,10,41,17,4,0)
VERSION=seal(_version)


def capture(**overrides):
    raw = bytearray(HISTORY)
    struct.pack_into('<I',raw,11,int(time.time())-60)
    struct.pack_into('<H',raw,15,16384)
    values = dict(id='fixture',source='history',rawBase64=base64.b64encode(seal(raw)).decode(),receivedAt=time.time(),deviceId='test')
    return SimpleNamespace(**(values | overrides))


def firmware(**overrides):
    return capture(**(dict(source='wire_frame', rawBase64=base64.b64encode(VERSION).decode(),
                           receivedAt=time.time()-3600) | overrides))


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));storage.initialize()
    with storage.connect() as db:
        yield db


def test_synthetic_firmware_and_history_frames_have_exact_conversion(db):
    temperature.record_firmware(firmware(receivedAt=1799999900),db)
    c=capture(rawBase64=base64.b64encode(HISTORY).decode(),receivedAt=1800000010)
    rows=temperature.samples(c,db)
    assert temperature.firmware_at(db,'test',c.receivedAt)=='41.17.4.0'
    assert rows[-1][1]==1800000000500
    assert rows[-1][2]==33.0  # word 330 at offset 76; firmware stores tenths C
    assert json.loads(rows[-1][0])['decoder']==temperature.DECODER
    assert temperature.observation(c)[1]['72']==1000  # different channel, not 40 C


def test_candidates_keep_original_time_without_firmware_or_user_calibration(db,tmp_path):
    (tmp_path/'temperature-calibration.json').write_text(json.dumps({'field_offset':72,'slope':.04,'intercept':0}))
    c=capture();rows=temperature.samples(c,db)
    assert len(rows)==7
    assert all(json.loads(r[0])['__name__']==temperature.RAW_METRIC for r in rows)
    assert {r[1]%1000 for r in rows}=={500}
    c.source='wire_frame'
    assert temperature.samples(c,db)==[]
    c.source='history';raw=bytearray(base64.b64decode(c.rawBase64));raw[5]=25
    c.rawBase64=base64.b64encode(seal(raw)).decode()
    assert temperature.samples(c,db)==[]


def test_firmware_gate_is_device_and_time_bound_and_handles_upgrades(db):
    c=capture();first=firmware();temperature.record_firmware(first,db)
    assert len(temperature.samples(c,db))==9
    c.deviceId='different'
    assert len(temperature.samples(c,db))==7
    c.deviceId='test'
    raw=bytearray(VERSION);struct.pack_into('<I',raw,18,5)
    change=firmware(receivedAt=time.time()-120,rawBase64=base64.b64encode(seal(raw)).decode())
    temperature.record_firmware(change,db)
    assert len(temperature.samples(c,db))==7
    # Receiving an old response later must not undo the upgrade gate.
    temperature.record_firmware(first,db)
    assert len(temperature.samples(c,db))==7
    assert temperature.firmware_at(db,'test',first.receivedAt-1) is None
    # A backfilled record from before the upgrade still uses the earlier firmware.
    raw=bytearray(base64.b64decode(c.rawBase64));struct.pack_into('<I',raw,11,int(time.time())-180)
    c.rawBase64=base64.b64encode(seal(raw)).decode()
    assert len(temperature.samples(c,db))==9


def test_unfamiliar_version_response_blocks_inheriting_old_firmware(db):
    temperature.record_firmware(firmware(),db)
    raw=bytearray(VERSION);raw[8]=2
    temperature.record_firmware(firmware(receivedAt=time.time()-120,rawBase64=base64.b64encode(seal(raw)).decode()),db)
    assert len(temperature.samples(capture(),db))==7


@pytest.mark.parametrize('word', [700,701,2550,65535,32768])
def test_sensor_error_and_out_of_operating_range_values_never_publish_degrees(db,word):
    temperature.record_firmware(firmware(),db)
    c=capture();raw=bytearray(base64.b64decode(c.rawBase64));struct.pack_into('<H',raw,76,word)
    c.rawBase64=base64.b64encode(seal(raw)).decode()
    rows=temperature.samples(c,db)
    assert len(rows)==8 and rows[-1][2]==0
    assert all(json.loads(r[0])['__name__']!=temperature.CELSIUS_METRIC for r in rows)


def test_skin_sensor_is_not_derived_from_pulse_or_adjacent_fields(db):
    temperature.record_firmware(firmware(),db)
    c=capture();raw=bytearray(base64.b64decode(c.rawBase64));raw[21]=0
    struct.pack_into('<H',raw,72,65535)
    c.rawBase64=base64.b64encode(seal(raw)).decode()
    assert temperature.samples(c,db)[-1][2]==33.0
    assert temperature.convert({'76':0})==0.0
    assert temperature.convert({'76':699})==69.9


def test_crc_fractional_clock_and_future_device_clock_are_checked(db):
    c=capture();raw=bytearray(base64.b64decode(c.rawBase64));raw[72]^=1
    c.rawBase64=base64.b64encode(raw).decode()
    assert temperature.samples(c,db)==[]
    struct.pack_into('<I',raw,11,int(time.time())+600)
    c.rawBase64=base64.b64encode(seal(raw)).decode()
    assert temperature.samples(c,db)==[]
    struct.pack_into('<I',raw,11,int(time.time())-60);struct.pack_into('<H',raw,15,32768)
    c.rawBase64=base64.b64encode(seal(raw)).decode()
    assert temperature.samples(c,db)==[]


def test_reference_pairing_requires_real_coverage_not_nearest_stale_value(db):
    from temperature_reference import pair
    at=int(time.time())-300
    for offset in temperature.OFFSETS:
        series=temperature.metric(temperature.RAW_METRIC,field=f'v24_{offset}')
        for second in range(-30,31):
            db.execute('INSERT INTO samples(series,timestamp,value) VALUES(?,?,?)',(series,(at+second)*1000,1000+offset))
    reference=pair(db,at,32.8,'Fixture probe')
    assert reference['fields']['72']['raw']==1072
    with pytest.raises(ValueError,match='covered'):pair(db,at+600,32.8,'Fixture probe')
    db.execute('DELETE FROM samples WHERE timestamp=?',((at+5)*1000,))
    db.execute('DELETE FROM samples WHERE timestamp=?',((at+6)*1000,))
    with pytest.raises(ValueError,match='missing'):pair(db,at,32.8,'Fixture probe')
