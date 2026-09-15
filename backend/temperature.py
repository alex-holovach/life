"""WHOOP 4 skin temperature, derived from Harvard 41.17.4.0 firmware.

MAX30208 -> signed counts * .005 C -> collector float -> round(C * 10)
-> signed little-endian word at absolute R24 frame offset 76.
See docs/TEMPERATURE.md for the complete data-flow trace and firmware hash.
No reference measurement or fitted coefficient participates in decoding.
"""
import base64
import json
import statistics
import struct
from whoop_protocol import decode

OFFSETS = (68, 70, 72, 74, 76, 78, 92)
RAW_METRIC = 'whoop_temperature_candidate_raw'
CELSIUS_METRIC = 'whoop_skin_temperature_celsius'
VALID_METRIC = 'whoop_skin_temperature_valid'
FIRMWARE = '41.17.4.0'
DECODER = 'harvard_41_17_4_0_r24_v1'


def record_firmware(capture, db):
    """Preserve timestamped device responses, including unsupported versions.

    Later-arriving old responses cannot overwrite a newer version. An unfamiliar
    REPORT_VERSION response blocks decoding instead of inheriting a prior version.
    """
    if capture.source != 'wire_frame':
        return
    raw = base64.b64decode(capture.rawBase64)
    frame = decode(raw)
    if frame is None or frame['layout'] != 'command_response' or frame['command_code'] != 7:
        return
    version = ('.'.join(map(str, struct.unpack_from('<4I', raw, 10)))
               if len(raw) == 84 and raw[8:10] == b'\x01\x01' else 'unknown')
    db.execute("""INSERT INTO firmware_observations VALUES(?,?,?,?)
        ON CONFLICT(device,observed_at) DO UPDATE SET
        firmware=CASE WHEN firmware=excluded.firmware THEN firmware ELSE 'unknown' END""",
        (capture.deviceId, capture.receivedAt, version, capture.id))


def firmware_at(db, device, at):
    row = db.execute('SELECT firmware FROM firmware_observations WHERE device=? AND observed_at<=? '
                     'ORDER BY observed_at DESC LIMIT 1', (device, at)).fetchone()
    return row[0] if row else None


def observation(capture):
    if capture.source != 'history':
        return None
    raw = base64.b64decode(capture.rawBase64)
    decoded = decode(raw)
    if decoded is None or decoded['layout'] != 'history_v24':
        return None
    at = struct.unpack_from('<I', raw, 11)[0] + struct.unpack_from('<H', raw, 15)[0] / 32768
    if not 1577836800 <= at <= capture.receivedAt + 300 or struct.unpack_from('<H', raw, 15)[0] >= 32768:
        return None
    return at, {str(i): struct.unpack_from('<H', raw, i)[0] for i in OFFSETS}


def convert(fields):
    word = fields['76']
    signed = word if word < 32768 else word - 65536
    # The collector initializes/resets its skin float to 70 C on sensor errors.
    # Suppress that ambiguous sentinel and values outside the MAX30208's stated
    # 0..70 C operating envelope. This is not a normal-body-temperature filter.
    return signed / 10 if 0 <= signed < 700 else None


def metric(name, **labels):
    return json.dumps({'__name__': name, 'device': 'strap', 'integration': 'whoop', **labels}, sort_keys=True, separators=(',', ':'))


def samples(capture, db):
    row = observation(capture)
    if row is None:
        return []
    at, fields = row
    result = [(metric(RAW_METRIC, field=f'v24_{key}'), int(at*1000), value) for key, value in fields.items()]
    # Do not apply today's firmware mapping to records predating our version evidence.
    if firmware_at(db, capture.deviceId, min(at, capture.receivedAt)) != FIRMWARE:
        return result
    value = convert(fields)
    result.append((metric(VALID_METRIC, decoder=DECODER), int(at*1000), int(value is not None)))
    if value is not None:
        result.append((metric(CELSIUS_METRIC, decoder=DECODER), int(at*1000), value))
    return result


def status(db, directory, now):
    device = db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
    firmware = firmware_at(db, device[0], now) if device else None
    candidates = []
    for offset in OFFSETS:
        series = metric(RAW_METRIC, field=f'v24_{offset}')
        row = db.execute('SELECT timestamp,value FROM samples WHERE series=? ORDER BY timestamp DESC LIMIT 1', (series,)).fetchone()
        if row:
            candidates.append(dict(field=offset, at=row[0]/1000, raw=row[1]))
    result = dict(state='awaiting_firmware' if firmware is None else 'unsupported_firmware',
                  celsius=None, observed_at=None, decoder=None, firmware=firmware,
                  quality=None, candidates=candidates, points=[])
    if firmware != FIRMWARE:
        return result
    result.update(decoder=DECODER, quality='firmware_decoded', state='awaiting_history')
    valid_series = metric(VALID_METRIC, decoder=DECODER)
    series = metric(CELSIUS_METRIC, decoder=DECODER)
    rows = db.execute('SELECT timestamp,value FROM samples WHERE series=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp',
                      (series, int((now-3600)*1000), int(now*1000))).fetchall()
    # One-minute means for display only; break lines at missing or invalid records.
    bins, segment, previous = {}, 0, None
    for row in rows:
        if previous is not None and row[0]-previous > 1500:
            segment += 1
        bins.setdefault((segment, row[0]//60000), []).append(row[1])
        previous = row[0]
    result['points'] = [dict(at=minute*60, celsius=statistics.mean(values), segment=segment)
                        for (segment, minute), values in bins.items()]
    latest = db.execute('SELECT timestamp,value FROM samples WHERE series=? AND timestamp<=? '
                        'ORDER BY timestamp DESC LIMIT 1', (valid_series, int(now*1000))).fetchone()
    if latest:
        result['observed_at'] = latest[0]/1000
        if now-result['observed_at'] > 600:
            result['state'] = 'stale'
        elif not latest[1]:
            result['state'] = 'sensor_unavailable'
        elif rows and rows[-1][0] == latest[0]:
            result.update(state='ready', celsius=rows[-1][1])
    return result
