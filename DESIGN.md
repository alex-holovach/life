# Life: iPhone collector and private telemetry backend

WHOOP 4 → iOS collector → private HTTPS over Tailscale → durable archive → Prometheus → Grafana.
A separate analysis service reads stored history and publishes results for the app and dashboard.
No WHOOP account, subscription, or cloud API is used. The current deployment is single-user
and binds one strap to a fixed metric alias.

See the [architecture diagram and feature coverage](README.md#architecture) and the
[Bluetooth sequence, GATT map and signal inventory](docs/BLUETOOTH.md).

## iPhone

The app shows live HR with local 5-minute, 30-minute and 1-hour charts; verified battery;
connection and export status; physiological results with their data-quality states; and a
link to the configured backend's Grafana dashboard. No personal backend address or signing
team is included in the public project. Configuration belongs in Settings and ignored local files.

Capture writes to SQLite before uploading. File-backed background URLSession tasks upload
batches; pending records survive network failures and app relaunch. CoreBluetooth restoration,
auto-reconnect and history backfill recover interrupted collection. An acknowledged stored-history
batch has been committed locally before its device cursor advances. iOS scheduling is not an
uninterrupted-runtime guarantee, and force-quit behavior remains subject to the platform.

Manual bedtime/wake actions and corrections are saved in a separate durable local journal.
They retry through authenticated, revisioned writes. A sleep journal is bound to its configured
backend so a URL change cannot silently send its pending edits to another installation.

## Backend and integration lifecycle

Run `./life start` from a persistent installation directory such as `~/life`. Docker Compose
starts API, remote-write worker, analysis, Prometheus, Grafana and gateway. The same command
includes additional integration Compose files under `integrations/`. Services use persistent
host directories and restart policies. See [operations](docs/OPERATIONS.md).

The gateway binds localhost and is exposed privately through Tailscale Serve. It routes Grafana,
health and explicitly allowed authenticated APIs. It does not expose `/metrics` or the
Prometheus administrative API. Tokens are generated during setup and mounted from local
secret files. Signing identity and host addresses are not portable source configuration.

Ingestion bounds request size, validates IDs and clocks, fsyncs compressed immutable batches,
commits SQLite references and an outgoing numeric queue, then acknowledges the request.
A separate worker sends eligible original-timestamp samples with Prometheus remote write.
Network failures retry with backoff; permanent errors and conflicting samples remain visible.

Raw history is retained for reprocessing. Prometheus is the numeric telemetry store, not the
only copy of original data. Default retention is 365 days with a configurable 30-day late-upload
window. Older samples remain archived for controlled historical import, without changing their
timestamps. Raw archives and phone records currently have no automatic pruning.

## Measurements and scoring

Examples of exported metrics include:

```
whoop_heart_rate_bpm{device="strap",source="live"}
whoop_battery_percent{device="strap"}
whoop_skin_temperature_celsius{device="strap",decoder="harvard_41_17_4_0_r24_v1"}
whoop_hrv_rmssd_ms{device="strap",algorithm="r24_rmssd_5m_v1"}
life_strain_score{device="strap",algorithm="life_edwards_hrmax_v1"}
life_sleep_duration_seconds{device="strap",algorithm="life_reported_duration_v1"}
```

Packet IDs, individual beat IDs, raw bytes and timestamps never become metric labels.
Analysis reads exact timestamped records, not Grafana's resampled chart points.

The incremental analysis service decodes firmware-supported pulse intervals, evaluates
five-minute HRV and respiratory windows, and retains rejected-window reasons. Versioned
oxygen results suppress ambiguous status codes. Temperature comes from the firmware-proven
skin field, without fitting a conversion to a personal thermometer reading. Missing or
unsupported values remain unavailable. See HRV.md, TEMPERATURE.md and RESPIRATION-OXYGEN.md
under `docs/` for evidence and physiological validation limits.

Cardio strain uses recorded HR and the explicitly configured maximum. Sleep duration uses
self-reported times and an explicitly configured target. Qualifying logged sleep intervals can
also provide sleeping HR, HRV, wrist temperature, and a multi-night temperature baseline.
See [scoring](docs/SCORING.md) for exact methods, thresholds and coverage rules.

Derived metrics are current-analysis scrape gauges with separate measurement timestamps.
Late backfill updates today's aggregate and recent sleep summaries; past scrapes are not
rewritten. Historical corrected-result export would require a separate versioned import policy.
There are no fixed score cards, automatic sleep stages, calorie estimates, or proprietary
WHOOP recovery/strain algorithms. Those require separate methods and validation.

## Validation and source release

Software tests cover protocol integrity, durable writes and retries, original-time replay,
quality gates, sleep edit conflicts, scoring arithmetic and daylight-saving boundaries.
Clinical accuracy is a separate question. See [validation](docs/VALIDATION.md).

Personal settings, credentials, telemetry, builds and backups are excluded from the current
tracked tree. A public release needs a reviewed clean export, a chosen license and new Git
history; the existing private history retains earlier deployment settings and presentation
experiments. See [public-source preparation](docs/PUBLIC-RELEASE.md).
