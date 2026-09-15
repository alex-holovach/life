"""WHOOP 4 framing and lossless research decoders. No inferred health measurements.

Offsets are frame-absolute. See docs/PROTOCOL-RESEARCH.md for provenance and
firmware limitations. Candidate fields must not be fed to health scoring.
"""
import base64
import math
import struct
import zlib


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (7 if value & 128 else 0)) & 255
    return value


class Frames:
    """Independent per-connection/channel reassembly; bounded even on malformed input."""
    def __init__(self):
        self.buffer = bytearray()
        self.discarded = 0

    def append(self, chunk):
        self.buffer.extend(chunk)
        result = []
        while len(self.buffer) >= 4:
            size = int.from_bytes(self.buffer[1:3], "little") + 4
            if self.buffer[0] != 170 or not 11 <= size <= 16384 or crc8(self.buffer[1:3]) != self.buffer[3]:
                del self.buffer[0]
                self.discarded += 1
                continue
            if len(self.buffer) < size:
                break
            result.append(bytes(self.buffer[:size]))
            del self.buffer[:size]
        return result


def decode(raw):
    """Decode complete CRC-valid frames, retaining uncertain scalars as offset-named raw values."""
    if (len(raw) < 11 or raw[0] != 170 or len(raw) != int.from_bytes(raw[1:3], "little") + 4
            or crc8(raw[1:3]) != raw[3]
            or zlib.crc32(raw[4:-4]) != int.from_bytes(raw[-4:], "little")):
        return None
    end = len(raw) - 4
    u16 = lambda i: struct.unpack_from("<H", raw, i)[0]
    u32 = lambda i: struct.unpack_from("<I", raw, i)[0]
    d = dict(packet_type=raw[4], version=raw[5], length=len(raw),
             layout="unknown", validation="uninterpreted", fields={}, waveforms={})
    f = d["fields"]

    def clock(offset):
        value = u32(offset)
        # Even epoch-looking values require comparison with receipt time before use.
        d["device_time_raw"] = value
        d["time_status"] = "epoch_candidate_unverified" if 1577836800 <= value <= 4102444800 else "non_unix_device_clock"

    if raw[4] == 40 and raw[5] == 2 and len(raw) == 28:
        d.update(layout="live_v2", validation="hr_layout_observed")
        clock(6)
        f.update(heart_rate_bpm=raw[12], subsecond_raw=u16(10),
                 byte_13_raw=raw[13], byte_22_raw=raw[22], byte_23_raw=raw[23])
        # Byte 13 is zero in this strap's captures. Do not invent an RR array here.
    elif raw[4] == 48 and end >= 16:
        d.update(layout="event", validation="event_layout_observed", event_code=u16(6))
        clock(8)
        f["subsecond_raw"] = u16(12)
        # Payload differs across firmware; preserve offset-named words, not guessed units.
        f.update({f"u16_{i}_raw": u16(i) for i in range(16, min(end - 1, 256), 2)})
        if u16(6) == 3 and len(raw) == 40:
            f["battery_decipct_candidate"] = u16(17)
            f["battery_mv_candidate"] = u16(21)
            f["charge_flags_candidate"] = raw[26]
    elif raw[4] == 36:
        d.update(layout="command_response", command_code=raw[6])
        # The leading response bytes include request sequence/status. No generic
        # success test or timestamp offset is assumed across commands.
        f.update({f"u8_{i}_raw": raw[i] for i in range(7, min(end, 15))})
    elif raw[4] == 49:
        d.update(layout="history_metadata", metadata_code=raw[6])
    elif raw[4] == 47 and raw[5] == 24 and len(raw) == 104:
        d.update(layout="history_v24", validation="research_layout_unverified")
        clock(11)
        n = raw[22]
        if n > 4:
            d.update(layout="unknown", validation="invalid_interval_count")
            return d
        f.update(heart_rate_candidate=raw[21], interval_count=n, byte_55_raw=raw[55])
        d["interval_words"] = [u16(23 + 2 * i) for i in range(n)]
        d["interval_status"] = "units_order_overlap_unverified"
        for i in (33, 35, 68, 70, 72, 74, 76, 78, 80, 82):
            f[f"u16_{i}_raw"] = u16(i)
        # Conflicting sources label 80/82 as respiration/quality; one measured them
        # constant. SpO2 remains unverified. The firmware-gated skin-temperature mapping is
        # implemented separately in temperature.py; these raw words stay unchanged.
        for i in (40, 44, 48, 56, 60, 64):
            value = struct.unpack_from("<f", raw, i)[0]
            if math.isfinite(value):
                f[f"f32_{i}_candidate"] = value
    elif raw[4] == 43 and raw[5] in (10, 11) and len(raw) in (1928, 1932):
        d.update(layout="raw_imu_candidate", validation="research_layout_unverified")
        clock(11)
        for label, offset in (("accel_x", 89), ("accel_y", 289), ("accel_z", 489),
                              ("gyro_x", 692), ("gyro_y", 892), ("gyro_z", 1092)):
            d["waveforms"][label + "_raw"] = list(struct.unpack_from("<100h", raw, offset))
        f["heart_rate_candidate"] = raw[21]
        # Keep ADC counts: published g/dps scales still need a controlled local test.
    elif raw[4] == 43 and raw[5] == 21 and len(raw) == 1244:
        d.update(layout="raw_optical_r21_candidate", validation="research_layout_unverified")
        clock(11)
        for label, offset in zip("abcdef", (24, 224, 424, 636, 836, 1036)):
            d["waveforms"][label + "_raw"] = list(struct.unpack_from("<100H", raw, offset))
        f.update(led_drive_candidate=u16(18), sample_count_raw=u16(20))
        # Channel identity, sample rate and oxygen calibration remain unverified.
    elif raw[4] == 43 and raw[5] == 17 and end >= 30:
        n = u16(28)
        if 0 < n <= 512 and 30 + n * 2 <= end:
            d.update(layout="raw_intervals_r17_candidate", validation="research_layout_unverified")
            clock(7)
            d["interval_words"] = [u16(30 + i * 2) for i in range(n)]
            d["interval_status"] = "units_order_overlap_unverified"
    return d


def capture_summary(capture):
    """Bounded summary for the latest-signal inventory; waveforms stay in the archive."""
    if capture.source != "wire_frame":
        return None
    result = decode(base64.b64decode(capture.rawBase64))
    if result is None:
        return None
    result["waveforms"] = {key: {"count": len(v), "min": min(v), "max": max(v)}
                           for key, v in result["waveforms"].items() if v}
    if "interval_words" in result:
        result["interval_count"] = len(result.pop("interval_words"))
    return result
