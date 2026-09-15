# Validation

Validation is incremental. A successful software test does not establish clinical accuracy.

## Reproducible checks

- `swift test`: protocol, reconnect/history handling, upload state, freshness, and sleep-journal retries.
- `swift run CoreChecks`: framing, CRC, fragmentation, durable replay, and battery cross-checks.
- `python3 scripts/check_backend.py`: isolated backend tests, including scoring and journal validation.
- `python3 scripts/check_deployment.py`: disposable Docker stack tests for persistence, replay, restarts,
  remote-write outages, and backup/restore. This uses synthetic inputs in an isolated installation.
- `python3 scripts/check_grafana_origin.py --url https://YOUR-TAILSCALE-HOST --username YOUR-USER`:
  real gateway cookie-session query, with rejection of untrusted origins.
- `python3 scripts/audit_public_source.py`: tracked-source checks for private deployment settings,
  runtime score constants, local data and credentials. See PUBLIC-RELEASE.md for history scanning.

Unit-test packet fixtures are constructed synthetic records. They are not bundled personal
telemetry and are never imported by the production analysis or UI.

## Hardware evidence and limits

WHOOP 4 heart rate, contemporaneous battery cross-checks, original-time history recovery,
reconnect, and short screen-off collection have been observed on an iPhone. CoreBluetooth
restoration and notification-recovery tests passed. This is not a promise of uninterrupted
Bluetooth or iOS background execution. Overnight and unplugged endurance remain ongoing
acceptance tests; whole-host reboot and replacement-host disaster recovery are separate drills.

The temperature, pulse interval and oxygen mappings have been checked against the matching
firmware with offline instruction execution. No proprietary firmware is distributed.
See TEMPERATURE.md, HRV.md and RESPIRATION-OXYGEN.md. Firmware behavior is distinct from
physiological accuracy. HRV lacks an ECG comparison; respiration and SpO₂ lack independent
reference validation. Unsupported and insufficient-quality measurements remain unavailable.

Strain is a documented Life cardio-load transformation, not WHOOP strain. Sleep duration is
self-reported. No automatic sleep-stage accuracy or proprietary recovery score is claimed.
No debug or Grafana presentation mode supplies fixed physiological results.

## Development builds

Install full Xcode and use your own signing identity in ignored `Config/Local.xcconfig`.
The public project contains no personal team, device identifier, host or prefilled credential.
Keep the bundle ID stable when updating an existing phone to preserve its local data.

```sh
xcodegen generate
xcodebuild -project Life.xcodeproj -scheme Life -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath build/DerivedData CODE_SIGNING_ALLOWED=NO build
```

Personal Team signing has a short provisioning lifetime. Reinstalling an update is subject
to Apple's signing, trust, Developer Mode, pairing and device-unlock requirements.

## Scoring release checks, September 14, 2026

85 backend tests and 25 Swift tests pass; CoreChecks pass. One optional external Prometheus
binary test is skipped locally. All 38 deployed dashboard PromQL expressions execute successfully.
Signed device and simulator builds pass, including an unsigned build from the clean source export
without local configuration. The updated application was installed and launched on the paired iPhone.

A monitored rolling backend update completed with 278 HTTP health checks and no errors.
The deployment keeps Grafana, Prometheus and the gateway running while replacing the API and
worker behind a temporary healthy API. These checks do not substitute for endurance testing.

Gitleaks reported no credential-pattern matches in the reviewed private history or clean source
snapshot. Earlier commits still contain personal deployment settings and old presentation values;
they must not be included in a public release. Synthetic protocol fixtures replaced personal
captured packets in the current test tree. No proprietary firmware binary is distributed.

## Past sleep entry and editing

The history UI supports entering missed nights and editing earlier entries. The new form
requires both times, previews net duration and validates overlaps, future times and awake
minutes before saving. Editing retains session identity and advances its revision.

30 Swift tests pass, including past entries, stale edits, overlaps, invalid input and elapsed
sleep across daylight-saving changes. All eight focused backend wellness tests pass,
including past-entry persistence, idempotent correction and preserving the latest sleep summary.
Simulator interaction verified calendar/time selection for a past night, duration preview,
saving offline, reopening from history and correcting awake minutes on the same entry.
The temporary Simulator journal was restored afterward; no test sleep was uploaded.
