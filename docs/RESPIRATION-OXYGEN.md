# Respiratory rate and oxygen

Life now indexes the device's oxygen result and computes an experimental respiratory estimate in the existing scheduled `analysis` service. The authenticated `/v1/vitals` endpoint, iPhone cards, and Grafana panels use these results. Missing, unsupported and ambiguous values remain unavailable. Last qualifying estimates are retained with their original measurement age.

Neither metric has been independently validated against a respiratory reference or pulse oximeter. No personal reference numbers or fitted calibration coefficients are used.

## Established oxygen mapping

The inspected binary is Harvard **41.17.4.0**, SHA-256 `2c4d7a9edf90e4b39bcecda28d2cd9949bd19de533a14d31a7409b31144c309b`, loaded at `0x10028000`. Addresses below are file offsets. The binary is not distributed with Life.

1. `0x86a08` is the routine called by the `SIGPROC:SPO2` logging path. It consumes two optical channels, calculates an AC/DC ratio, applies the device's coefficients and returns a byte containing either a saturation result or a status code.
2. Caller around `0x7f872` writes the return byte to algorithm output `+0x221`. The accompanying floating-point ratio is at `+0x224`.
3. Producer around `0x60458` copies `+0x221` to the collector event. It **does not copy the ratio**.
4. Event consumer `0x35f0c` copies event `+0x221` to collector `+0xb6a`.
5. Serializer `0x34802` writes collector `+0xb6a` to absolute R24 frame offset **86**.

The sleep-related scheduling path logs successful oxygen readings during sleep and escalation when none have been obtained during the first three hours. This establishes a firmware scheduling path; it does not prove that a particular night will produce a usable result with Life.

### Status and ambiguity

- `0`: no computed reading; never 0% saturation.
- `1`, `2`, `3`: results outside the routine's accepted output range.
- Error bits include `4`, `8`, `16`, `32`, `128`; combinations are not percentages.
- The routine accepts integer results from `70` through `100`, but **98 is ambiguous**.
- At `0x86cc0`, either missing optical DC denominator can produce **98**, even though no usable ratio is available. R24 omits the ratio diagnostic needed to distinguish this fallback from a real 98. Life therefore withholds all 98 results. This also loses genuine 98 readings; do not calculate an overnight mean from this filtered series.

`scripts/verify_oxygen_firmware.py` executes the actual ARM conversion tail and event-to-packet copies in Unicorn. Eleven cases cover actual ratio outputs, out-of-range status codes, missing DC returning 98, and error-flag overrides. This verifies software behavior, not the accuracy of the sensor or the coefficients.

Only CRC-valid 104-byte R24 records with valid device timestamps and timestamped firmware evidence are eligible. Conflicting records at the same device timestamp are withheld. The UI shows the most recent unambiguous device result within 24 hours, with its original observation time. A subsequent zero means no new measurement; it does not retimestamp the previous result. Unsupported current firmware or a stopped analysis worker suppresses values.

## Respiratory estimation

No computed respiratory-rate field has been established. In particular, R24 words at offsets **78, 80 and 82 are optical channel configuration bitfields**, not breathing rate. Function `0x7f210` packs channel index and configuration bits; the disabled encoding is `channel | 0x0c00`. Unknown output fields remain unknown.

Algorithm `r24_pulse_respiration_v1` uses the same firmware-proven, chronological millisecond pulse intervals as HRV, not heart-rate graph points and not the HRV scalar. It looks for respiratory modulation of pulse timing:

1. Inspect trailing five-minute history windows every minute. Split at missing or out-of-order records, sequence discontinuities, empty interval records, clamped intervals, invalid HR and conflicting records. Empty records can be legitimate at slow heart rates; treating them as breaks intentionally reduces availability.
2. Require at least **90 contiguous seconds** and interval duration coverage between **98% and 102%**. Reject local interval outliers beyond 20% of the neighboring median and changing HR across 30-record blocks.
3. Reconstruct relative beat times by summing intervals within that segment. Remove a linear trend and require at least 3 ms RMS remaining modulation.
4. Fit sinusoids directly to the uneven beat times over 6–30 breaths/min. Require a clear peak explaining at least 50% of the detrended variance, with at least 1.8 times the power of competing frequencies more than 0.04 Hz away.
5. Cross-check against autocorrelation of a 4 Hz interpolation **within the already-contiguous segment**. Require agreement within 1.5 breaths/min and a strong periodic peak. Require estimates from both halves to agree within 2 breaths/min. Reject search-boundary and inadequate sampling-rate cases.
6. Publish only if all screens pass. Mark stale after 15 minutes measured from the actual segment end; retain the last qualifying estimate for 24 hours with its original age. The UI rounds to whole breaths/min. Grid spacing is not measurement accuracy.

These are engineering gates. Frequency agreement does not prove that the modulation comes from respiration, and two methods applied to the same signal are not independent reference validation. Motion, autonomic rhythms, harmonic ambiguity, weak respiratory sinus arrhythmia, irregular beats, and sparse intervals can prevent or mislead an estimate. No confidence percentage or medical accuracy is claimed. The cards say **estimated** and retain a reference-validation notice.

Tests recover known synthetic 8, 12 and 20 breaths/min signals and reject flat signals, noise, changing frequency, gaps, empty records, clamps and conflicts. Synthetic tests do not establish performance on people.

## Operation and persistence

- No additional service or command is needed. `./life start` starts the existing stack, including analysis.
- The versioned archive cursor replays existing raw history at upgrade. New history and late backfill update indexes and affected windows automatically. Firmware evidence changes trigger recomputation.
- Raw oxygen codes and original timestamps are retained in `oxygen_records`. Versioned respiratory results and rejected-window reasons remain in `respiration_windows`.
- Respiratory numerical work runs outside the SQLite write transaction so it does not block ingestion for the duration of the calculation.
- Prometheus receives current analysis gauges, with decoder/algorithm labels and separate original measurement timestamps. Graph timestamps are analysis observation times, **not newly collected historical measurement times**. SQLite retains revised historical analysis. Missing values do not become zero.
- API requests require the existing bearer token; the gateway exposes only the explicit `/v1/vitals` route. `/metrics` stays private. Mobile requests do not follow redirects with credentials.

## Remaining evidence needed

Capture a nonzero, unambiguous device oxygen result and verify its behavior. Investigate whether the ratio/quality diagnostic can be obtained through a documented-by-observation reversible channel before allowing 98. Establish channel identities before attempting any independent red/IR calculation. Do not apply a generic calibration curve to unknown optical channels.

For respiratory accuracy, compare qualifying recordings against an independent respiratory reference over multiple sessions and rates, reporting failures as well as error. Collection can continue independently of reference testing.

Sources:

- [WHOOP Harvard firmware release](https://github.com/tanarchytan/whoop-firmware/releases/tag/harvard-41.17.4.0), inspected offline; see addresses and hash above.
- [Charlton et al., assessment of respiratory-rate algorithms](https://peterhcharlton.github.io/RRest/yhvs_assessment.html): original comparison using ECG/PPG and a respiratory reference; this implementation is not claimed to reproduce a validated algorithm from that study.
- [Analog Devices, pulse-oximeter measurement and calibration](https://www.analog.com/en/resources/technical-articles/how-to-design-a-better-pulse-oximeter.html): identifiable red/IR optical signals and sensor-specific calibration are needed for an independent calculation.


## Availability and failure states

The API retains the last qualifying respiratory estimate for 24 hours from its
actual segment end, even when later attempts fail. Results older than 15 minutes results
are `stale`, never `ready`. The app and Grafana label this as the last estimate
and show its original time/age. Separate `whoop_respiratory_rate_last_qualified_*`
gauges support that display; the existing live metrics keep their 15-minute gate.
A stopped analyzer or unsupported firmware still suppresses results.

`whoop_respiratory_rate_signal_status` is a bounded diagnostic code: 0 no qualifying
recording, 1 ready, 2 stale, 3 fragmented timing, 4 missing pulse duration, 5 artifacts
or changing HR, 6 inconsistent respiratory modulation, 7 analysis unavailable,
8 unsupported firmware. Grafana maps these codes to text when no estimate exists.
No diagnostic code is presented as a measured breathing rate.

Empty batches can be legitimate at slow HR, but can also reflect withheld interval
output. Joining them without continuity evidence does not establish a breathing
value. Collection time alone does not guarantee a qualifying estimate.

Backfill status uses the latest window ending within downloaded history, rather than
a future window containing only the tail of an incomplete transfer. The API includes
`history_end`, and the mobile UI distinguishes delayed history from signal rejection.
The estimator and its quality thresholds are unchanged. Repeated identical firmware
reports no longer restart the historical analysis queue; see [HRV scheduling](HRV.md#backfill-diagnostics-and-scheduling).

The shared 24-hour quality report now shows how many respiratory windows were analyzed
and how much time actually qualified. Zero qualifying coverage remains distinct from
zero breathing rate. The iPhone explains that empty pulse batches can occur at slow
heart rates; "fragmented" alone was liable to imply a Bluetooth failure even when
all history records arrived. The respiratory estimator remains unvalidated.
