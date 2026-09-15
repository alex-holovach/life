"""Small Remote Write 1.0 encoder; timestamps are original Unix milliseconds.
Schema: https://prometheus.io/docs/specs/prw/remote_write_spec/
No intervals are exported until device units/timing have been independently validated.
"""
import json
import struct
from collections import defaultdict
import cramjam


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def encode(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["series"]].append((row["timestamp"], row["value"]))
    request = bytearray()
    for series, samples in sorted(groups.items()):
        labels = json.loads(series)
        message = b"".join(field(1, field(1, k.encode()) + field(2, v.encode())) for k, v in sorted(labels.items()))
        for timestamp, value in sorted(samples):
            sample = b"\x09" + struct.pack("<d", value) + b"\x10" + varint(timestamp)
            message += field(2, sample)
        request += field(1, message)
    return bytes(cramjam.snappy.compress_raw(request))
