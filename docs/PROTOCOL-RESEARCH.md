# WHOOP 4.0 sensor capture and decoding

For current support, see the [Bluetooth map and signal inventory](BLUETOOTH.md).

Status: 2026-09-14 UTC. Device firmware previously identified as 41.17.4.0.
This document separates observations from candidate protocol mappings. Skin temperature
is now decoded from the matching firmware; see [the complete trace](TEMPERATURE.md).
SpO2 and respiration remain unvalidated. R24 interval units/order are now firmware-proven and PPG RMSSD is implemented; independent ECG validation remains. See [HRV](HRV.md).

## Recording interpretation

Live frames, event frames and command responses use different layouts. Raw frames
remain available for later decoding. Live measurements use phone receipt time;
history requires a verified device clock and retains the original record time.

Standard BLE interval words remain excluded from HRV because their units and
continuity are unverified. Comparing intervals with averaged heart rate does not
resolve either question. Additional battery-event fields remain candidates;
normal battery publication still requires the established cross-check.

## Implemented capture paths

- iOS inventories all GATT services and characteristics, including their properties.
- Optional standard Device Information (180A), Health Thermometer (1809) and Pulse
  Oximeter (1822) services are read/subscribed if present. Their presence on this
  firmware is not assumed. Unknown bytes stay in the archive.
- Every complete CRC-valid custom frame is stored as `wire_frame`, in addition to
  the original notification fragments. This permits decoding after upload without
  reconstructing packets from an arbitrary subset of fragments.
- Settings → Sensor research → Capture additional sensors runs a 65-second trial:
  status/configuration reads; one unacknowledged history batch; 15 seconds of motion;
  20 seconds of wrist-gated optical data; then cleanup.
- Automatic history ACK (23) now requires durable storage, valid framing, contiguous
  v24 record counters and the verified end-marker layout. No trim, read-pointer change,
  calibration, firmware update, forced optical mode or persistent R21 command is sent.
- The trial stops on leaving the foreground. A persisted cleanup flag makes the next
  connection disable extra streams if the process or connection was interrupted.
  Cleanup write completion is recorded; actual stream cessation still needs checking
  on this firmware. The test does not promise a device-side timeout while disconnected.
- `scripts/capture_sensors.py` provides the same experiment from an already authorized
  Bluetooth adapter, with notification/command recording and a post-cleanup observation.

Both capture paths preserve command requests and responses. A successful BLE write is
transport acknowledgement, not proof that an undocumented command took effect.

## Decoder coverage

`backend/whoop_protocol.py` verifies lengths and both checksums before interpretation.
All offsets below are absolute in the framed packet, including the four-byte header.

| Layout | Decoder behavior | Validation remaining |
| --- | --- | --- |
| 0x28 / v2 / 28 bytes | HR, raw device/subsecond clock, unnamed status bytes | Status meanings, clock synchronization |
| 0x30 events | Event ID, clock and bounded raw payload words | Firmware-specific payload semantics |
| 0x24 responses | Command ID and raw prefix | Command-specific status/response schemas |
| 0x31 metadata | Metadata code, original frame | History sequence/ACK validation |
| 0x2F / v24 / 104 bytes | HR, raw intervals, raw sensor words; firmware-gated skin temperature in temperature.py | Independent HRV accuracy and remaining sensor labels |
| 0x2F / v25 / 84 bytes | Initial optical sample plus 24 differences; reconstruct only without saturation | [Firmware trace](OPTICAL.md); wavelength, exact sample timing and health use remain unvalidated |
| 0x2B / v10–11 / 1928–1932 bytes | Six candidate 100-sample signed channels | Record discrimination, sample rates, axes, g/dps scaling |
| 0x2B / v21 / 1244 bytes | Six candidate 100-sample unsigned optical channels | Channel identities, sample rate, calibration |
| 0x2B / v17 | Bounded interval words | Hardware support, length, ordering, units |
| Other layouts | Original frame, type, revision, length | Identify from new recordings |

The public maps disagree about R11/optical layouts, temperature scaling and which
optical channels are red/IR. Fields at offsets 80/82 described as respiration and
signal quality in one source are reported constant in another. Life retains them as
`u16_80_raw` / `u16_82_raw`, not breathing rate or a physiological quality score.

SpO2 needs confirmed red/IR signals with the appropriate AC/DC information and
calibration, or a verified device-produced SpO2 measurement. A green-only waveform,
HR, an LED-drive setting or a plausible-looking ADC ratio is insufficient. Skin
temperature is decoded separately with the firmware-derived conversion in
[TEMPERATURE.md](TEMPERATURE.md). No guessed percentages, sleep stages or health
scores are emitted by these decoders.

## Run and inspect

From the Life repository:

```sh
python3 scripts/analyze_capture.py /path/to/captures.sqlite --output /path/to/report.json
python3 scripts/analyze_capture.py /path/to/backend/archive --output /path/to/report.json
uv run --with bleak python scripts/capture_sensors.py \
  --discovery data/discovery.json --output work/sensor-session.jsonl
python3 scripts/analyze_capture.py work/sensor-session.jsonl --output work/sensor-report.json
```

Analysis is read-only and deduplicates captures before reassembly. Streams are separated
by device, connection session and characteristic. Reports include clock ranges, field
variance, waveforms, replies, GATT inventory and interval completeness. Keep reports and
raw captures private; neither belongs in Git.

Authenticated `GET /v1/sensors` returns the latest decoded frame summary and count for
each bounded layout name. Waveforms remain in the archive; summaries include their
count/min/max. Late arrivals do not replace newer summaries, and replay does not inflate
counts. Existing archive data can be examined using the offline analyzer. The endpoint
starts indexing new `wire_frame` uploads after this update; it does not rewrite history.

## Validation and remaining hardware work

Software tests exercise CRC rejection, fragmentation, unknown revisions, malformed
interval counts, non-finite floats, waveform bounds, authentication and replay.
Hardware research should verify stream cleanup, controlled motion and independent
pulse/oxygen references. Firmware establishes skin-temperature units, while
physiological accuracy needs separate validation. Personal recordings stay local.

## Sources and attribution

Protocol facts and candidate offsets were compared against these primary research repositories:

- [OpenStrap research](https://github.com/OpenStrap/research/tree/c6be09c4021546e4693dcf74da25d7b793ff8a40),
  especially `PROTOCOL.md` and `research_playground.py`. MIT notice retained in
  `docs/licenses/OpenStrap.txt`.
- [NOOP research](https://github.com/0xmts/noop-latest/blob/63f3ed06cc2ff1c3d1e2d0e6828bcfe7387ae521/docs/BLE_REVERSE_ENGINEERING.md),
  compared as a conflicting protocol reference, not adopted as validated health algorithms.
- [Bluetooth Heart Rate Service](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html).

Research claims about other straps or firmware remain candidate mappings until verified.

## Temperature and mixed-history follow-up

See [TEMPERATURE.md](TEMPERATURE.md) for the MAX30208 firmware evidence, raw
fields and the firmware-derived Celsius decoder. The earlier history fix checks v24
and v25 counters separately and archives both before ACK. R25 now has an offline
research waveform decoder; see [stored optical waveform research](OPTICAL.md).
