import struct
import zlib
import pytest
from whoop_protocol import Frames, crc8, decode

def frame(kind, version, size):
    raw = bytearray(size)
    raw[0] = 170
    struct.pack_into("<H", raw, 1, size - 4)
    raw[3] = crc8(raw[1:3]); raw[4] = kind; raw[5] = version
    return raw


def seal(raw):
    raw[-4:] = zlib.crc32(raw[4:-4]).to_bytes(4, "little")
    return bytes(raw)


# Constructed protocol fixtures, never copied from a personal recording.
_live=frame(40,2,28);struct.pack_into('<I',_live,6,30_000_000);_live[12]=75
LIVE=seal(_live)
_battery=frame(48,1,40);struct.pack_into('<H',_battery,6,3)
struct.pack_into('<H',_battery,17,500);struct.pack_into('<H',_battery,21,3800)
BATTERY=seal(_battery)


def test_synthetic_live_frame_keeps_non_unix_clock_out_of_health_timestamps():
    d = decode(LIVE)
    assert d["layout"] == "live_v2"
    assert d["fields"]["heart_rate_bpm"] == 75
    assert d["device_time_raw"] == 30000000
    assert d["time_status"] == "non_unix_device_clock"
    assert "interval_words" not in d
    assert "spo2" not in str(d)


def test_synthetic_battery_frame_is_preserved_as_candidate_not_verified_measurement():
    d = decode(BATTERY)
    assert d["event_code"] == 3
    assert d["fields"]["battery_decipct_candidate"] == 500
    assert d["fields"]["battery_mv_candidate"] == 3800


def test_reassembly_handles_every_split_embedded_marker_corruption_and_reconnect():
    for split in range(len(LIVE) + 1):
        parser = Frames()
        assert parser.append(LIVE[:split]) + parser.append(LIVE[split:] + BATTERY) == [LIVE, BATTERY]
    parser = Frames()
    assert parser.append(b"bad!" + LIVE) == [LIVE]
    assert parser.discarded == 4
    corrupted = bytearray(LIVE); corrupted[12] ^= 1
    assert decode(corrupted) is None
    for n in range(len(LIVE)):
        assert decode(LIVE[:n]) is None
    # Independent reassemblers never splice frames across different BLE sessions.
    first, second = Frames(), Frames()
    assert not first.append(LIVE[:10])
    assert second.append(LIVE) == [LIVE]


def test_unknown_record_revision_never_uses_a_known_sensor_map():
    raw = frame(47, 25, 104)
    assert decode(seal(raw))["layout"] == "unknown"
    raw[5] = 24; raw[22] = 255
    assert decode(seal(raw))["validation"] == "invalid_interval_count"


def test_history_sensor_candidates_do_not_turn_constant_words_into_respiration():
    raw = frame(47, 24, 104)
    raw[22] = 2; struct.pack_into("<2H", raw, 23, 782, 801)
    struct.pack_into("<H", raw, 80, 3073)
    struct.pack_into("<f", raw, 40, float("nan"))
    d = decode(seal(raw))
    assert d["interval_words"] == [782, 801]
    assert d["fields"]["u16_80_raw"] == 3073
    assert "f32_40_candidate" not in d["fields"]
    assert d["validation"] == "research_layout_unverified"
    assert not any("resp" in k or "temperature_c" in k for k in d["fields"])


@pytest.mark.parametrize("kind,version,size,channels", [(43,10,1928,6),(43,21,1244,6)])
def test_waveforms_are_bounded_raw_samples_not_calibrated_values(kind,version,size,channels):
    raw = frame(kind,version,size)
    d = decode(seal(raw))
    assert len(d["waveforms"]) == channels
    assert all(len(v) == 100 for v in d["waveforms"].values())
    assert all(k.endswith("_raw") for k in d["waveforms"])
    raw[5] = 255
    assert not decode(seal(raw))["waveforms"]
