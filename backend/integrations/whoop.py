"""Validated numeric WHOOP fields. Unvalidated fields remain in the raw archive."""
import json

def samples(capture):
    c = capture
    at = int((c.sampleAt if c.sampleAt is not None else c.receivedAt) * 1000)
    def metric(name, value, **labels):
        return (json.dumps({"__name__": name, "device": "strap", "integration": "whoop", **labels}, sort_keys=True, separators=(",", ":")), at, value)
    result = []
    if c.source in ("live_hr", "history") and c.hrBpm is not None and 0 < c.hrBpm <= 255 and c.quality in ("live_receipt_timestamp", "historical_intervals_unverified"):
        result.append(metric("whoop_heart_rate_bpm", c.hrBpm, source="live" if c.source == "live_hr" else "history"))
    if c.source == "battery_verified" and c.quality == "cross_checked" and c.batteryPercent is not None:
        result.append(metric("whoop_battery_percent", c.batteryPercent))
    # Acceleration layout/scaling and pulse intervals remain archived pending validation.
    return result
