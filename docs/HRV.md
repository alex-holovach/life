# PPG-derived HRV

Life calculates five-minute RMSSD from the WHOOP 4's stored pulse intervals. It
never derives HRV from averaged heart rate and never mixes live BLE intervals with
history. 

## What is established

Firmware: Harvard 41.17.4.0, decompressed SHA-256
`2c4d7a9edf90e4b39bcecda28d2cd9949bd19de533a14d31a7409b31144c309b`.
The binary is available in the [firmware research release](https://github.com/tanarchytan/whoop-firmware/releases/tag/harvard-41.17.4.0)
and is not distributed with Life. Runtime load base: `0x10028000`. Addresses below
are offsets in the decompressed binary, not runtime addresses.

- `0x8a374`: processes a 400-sample optical signal, advancing 100 samples per call.
  Peak locations are chronological; `0x8a690` performs local peak interpolation.
  `0x8a714–0x8a758` subtracts successive interpolated peak locations and divides
  by **104 samples/second**, producing interval seconds.
- The persistent cursor retains the previous peak as the next window's starting
  peak. Empty output is possible even for a clean signal. Concatenating an empty
  interval notification with distant data is not proof of beat continuity.
- `0x88980`: interval extraction and bounds. Firmware may clamp intervals to
  1/3 or 1/2 second at the lower end and 2.4 seconds at the upper end. We exclude
  ambiguous clamp values and substantial deviations rather than treating every
  stored interval as a confirmed normal-to-normal ECG interval.
- `0x86780`: clears the output each call, and only copies the interval output
  when its internal signal/motion criterion is below 0.03. The criterion's precise
  physical meaning is not established here. Thus no intervals does not mean no pulse.
- `0x7f68a–0x7f6cc`, `0x7f97c–0x7f99e`: caps the count at four and stores
  `floor(seconds * 1000 + 0.5)` as little-endian uint16, in the same order.
- `0x604b4` sends that packed structure in signal-processing event 0x90;
  `0x35e40` copies it into collector offset 0x154.
- `0x3472e–0x34744` copies count + four words into R24 absolute bytes 22–30.
  Frame type 0x2F, version24, exact length104. Sequence is bytes7–10;
  record time is seconds at11–14 plus fraction at15–16 /32768.

`verify_hrv_firmware.py` executes the actual instructions in Unicorn: six
packing cases (including rounding and four-value truncation), the R24 copy,
40 successive synthetic signal windows, and 120-window clean-signal sweeps at
45, 50, 55, 60, 75 and 100 bpm. The sweeps check continuity across naturally empty
batches and record the spread of publication phase. They do not establish bounds
under motion, changed pulse morphology or output gating. It checks peak order, interval
scale, continuity across calls, and a legitimate empty output. This proves the
investigated software data path, not the physiological accuracy of the optical sensor.

## Calculation and gates

`RMSSD = sqrt(mean((interval[i] - interval[i-1])²))`, in milliseconds.
Windows are exactly 300 seconds, ending each UTC minute. All arithmetic uses the
original integer millisecond values. Display rounds to whole milliseconds, with
no implication of 1 ms physiological accuracy. The 104 Hz signal is interpolated;
1 ms is the **encoded resolution**, not an independently validated error bound.

A window must satisfy these versioned, conservative engineering rules:

- CRC-valid R24 packets, a valid device clock and matching timestamped firmware
  evidence covering the entire window. Unsupported firmware blocks calculation.
- Contiguous record sequence including uint32 rollover; cadence 0.90–1.05 seconds
  (nominal100/104). Both window boundaries must be covered within1.05 seconds.
  Conflicting records at the same timestamp block that window.
- Accepted interval duration must cover98–102% of the300-second window. Boundary
  phase can add/subtract an interval; this is a coverage screen, not a beat timestamp.
- Intervals strictly between333 and2000 ms, excluding500 ms as an ambiguous firmware
  clamp. Reject deviations over20% from the local11-interval median within a continuous
  segment. At most1% of recorded intervals may be rejected.
- At least180 adjacent pairs and at least90% of the potential pairs retained.
  Empty interval records, sequence/time gaps and rejected intervals break adjacency.
  We never join the two surviving neighbors of an excluded beat or interpolate replacements.
- Mean heart rates for all five minute blocks must exist and differ by at most10 bpm.
  This screens out changing heart rate; it does not establish posture, sleep, or rest.

These thresholds are product choices, not published clinical acceptance criteria.
They can reject real physiological variability as well as artifacts. Version any
future change. PPG measures pulse variability, not ECG R-R intervals; comparison
against an independent ECG-quality reference remains necessary before claiming
precise physiological HRV or equivalence to WHOOP's overnight metric.

The five-minute duration and RMSSD definition follow the
[ESC/NASPE Task Force](https://www.escardio.org/static-file/Escardio/Guidelines/Scientific-Statements/guidelines-Heart-Rate-Variability-FT-1996.pdf)
and [Laborde et al. methodological recommendations](https://pmc.ncbi.nlm.nih.gov/articles/PMC5316555/).
No recommendation is used as evidence that this particular sensor has been validated.

## Running and storage

`./life start` includes the new `analysis` service. It scans newly archived history
using a durable cursor, indexes records in SQLite, and recomputes affected windows
every30 seconds. A process lock prevents concurrent analyzers. Archive I/O occurs
outside the database write transaction. Late backfill and newly arriving firmware
evidence revisit affected windows, including previously rejected results. The
original archives remain unchanged and recalculation does not replace raw telemetry.

`GET /v1/hrv` uses the ingestion bearer token. It returns the latest qualifying
window, its actual end time, interval coverage and pair count, plus the latest
attempt and rejection reasons. The live state expires 15 minutes after the **window end**. The app and dashboard retain the last qualifying reading for 24 hours, explicitly labeled with its original time or age. This is a five-minute measurement, not a whole-night average.
An expired analyzer heartbeat or unsupported current firmware hides the reading.
The app refreshes while active and rechecks freshness independently.

Prometheus scrapes `whoop_hrv_rmssd_ms`, `whoop_hrv_ready`, coverage, pair count and
window-end timestamps from the API. These are analysis gauges at scrape time:
the original measurement end is the separate `whoop_hrv_window_end_timestamp_seconds`.
This avoids overwriting immutable Prometheus samples when late backfill improves
an old window. SQLite keeps the recomputed historical windows; Prometheus records
what the analyzer knew at each scrape. Backfilling old raw history does not inject
retroactive HRV points into Prometheus. Grafana uses separate `whoop_hrv_last_qualified_rmssd_ms` and `whoop_hrv_last_qualified_end_timestamp_seconds` gauges for the last qualifying reading. The existing live metric retains its 15-minute readiness gate.
All HRV metrics carry the bounded `algorithm="r24_rmssd_5m_v1"` label.

Inspect the service with `./life logs analysis`. An analyzer heartbeat older than
180 seconds fails its health check and suppresses current readings. Docker restarts
an exited analyzer automatically, just like the existing API and worker.

## Validation scope

Firmware execution establishes encoded units, order and rounding. Software tests
cover scoring gates, replay, timestamps and persistence. API and Prometheus output
have been compared, but no simultaneous ECG reference validates sensor accuracy.
Personal recording results are retained outside this repository.

## Backfill diagnostics and scheduling

The latest-attempt diagnostic excludes windows ending beyond the downloaded history
(with the existing 1.05-second boundary tolerance). A trailing fragment during backfill
must not be presented as a completed five-minute signal-quality assessment. The API's
`history_end` retains the last indexed record time; the app and dashboard expose delay.
The RMSSD calculation and all acceptance thresholds are unchanged.

Repeated reports of the same firmware no longer invalidate every historical window.
Only changes to the version timeline, including late evidence moving its first boundary,
trigger recomputation. Pending windows are processed between short one-second yields;
the usual 30-second idle wait applies once the eligible queue is empty.

## Availability audit

The `/v1/hrv` response includes a cached `quality_report` for the past 24 hours:
consecutive-record coverage, empty interval-record count, analyzed/qualified window
counts, rejection reasons, and qualifying values at their actual measurement times.
The iPhone shows a coverage card and a scatter plot without connecting gaps. Grafana
exposes the same coverage and window counts. Coverage is availability, not a
physiological confidence percentage. Overlapping accepted windows contribute their
union of covered time; they are not independent recordings. Reports expire if the
analyzer or report stops updating, and unknown current firmware suppresses them.

A deterministic record-cadence test exposes a limitation in v1: complete synthetic
50 and 55 bpm signals with alternating intervals and 20 ms RMSSD fail only the
adjacent-pair rule, while 60 bpm passes. Natural empty batches at the 100/104-second
record cadence split pairs even when pulse-duration coverage is complete. This can
reduce overnight availability at low HR. An empty batch can also mean the firmware
withheld intervals, so it is not safe to concatenate indiscriminately. Until those
cases can be distinguished, the implementation preserves its current numerical
rules and reports the continuity limitation separately from missing history or
artifacts. More wear time alone cannot guarantee an overnight HRV result.
