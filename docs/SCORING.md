# Scoring and sleep journal

Life computes its own cardio-load and reported-duration scores. These are not WHOOP's
proprietary strain, recovery, or sleep-performance algorithms. Missing measurements
stay unavailable in both the app and Grafana. No example scores are shipped in either build.

## Cardio strain

Set your known maximum heart rate in the app's **Strain & sleep → Set up** screen.
There is no default age, estimated maximum, or assumed resting heart rate.
Saving also sets the daily time zone to the phone's current zone. Travel does not
silently change an existing day's boundary; save settings to change it deliberately.

The backend integrates consecutive firmware-supported history records, using their
original device timestamps. Intervals count only when sequence numbers are consecutive,
timestamps are 0.90–1.05 seconds apart, the record is unconflicted, and HR is nonzero.
Missing intervals are not interpolated or filled forward. Late backfill recomputes
today's total from the stored records. At least ten minutes must be recorded.

Edwards load is minutes at 50–60%, 60–70%, 70–80%, 80–90%, and at least 90% of maximum HR,
weighted by 1, 2, 3, 4, and 5 respectively. Below 50% contributes zero load.
See the method in [this primary research paper](https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2017.00878/full).
This uses percentage of **maximum HR**, not heart-rate reserve.

Life's separate display transform is `21 × ln(1 + load) / ln(1 + 5 × day_minutes)`.
This is a documented design choice, not a validated physiological scale or WHOOP's formula.
A local day can be 23, 24, or 25 hours. The display always includes the fraction of elapsed
day actually recorded; below 90% is marked partial. It measures cardiovascular load, not
muscular strain, calories, or all forms of exercise stress. Algorithm: `life_edwards_hrmax_v1`.

## Sleep

Tap **Going to sleep**, then **I'm awake**. **Edit times** corrects the current or latest
entry. To enter a missed night, tap **Log past sleep**, choose both dates and times, enter
minutes awake, then review the duration and save. New entries have no assumed sleep times.
**Sleep history** lists saved entries with duration, dates and sync status; tap any entry
to edit it. Changes keep the same session ID and increment its revision, so retries cannot
create duplicates. Date validation catches overlaps, future times and incorrect overnight
dates before saving. Durations use elapsed time across daylight-saving changes.

The history screen includes entries saved on this phone and the latest 100 entries received
from the backend. Older entries remain stored on the backend. Adding a past night leaves
an ongoing sleep session unchanged unless the intervals overlap. The phone saves edits atomically before attempting upload; queued
edits survive app restarts and retry with the same UUID and revision. The backend rejects
conflicting revisions and overlapping sessions. It retains an edit audit in SQLite, included
in normal backups. An interrupted network request cannot create a duplicate sleep session.

Recorded sleep duration is `wake − bedtime − reported awake time`. Set your own target in
hours. The duration score is `min(100, 100 × duration / target)`. Without a target, duration
is available but the score is not. This is self-reported duration, not detected sleep,
sleep stages, sleep efficiency, or a claim of sleep quality. Sessions are limited to 24 hours.
Algorithm: `life_reported_duration_v1`.

A logged session can also provide:

- Median sleeping HR, after 30 minutes and at least 80% recorded coverage.
- Median qualifying five-minute HRV windows, with at least three windows and 50% combined
  time coverage. Overlapping windows are counted only once for coverage.
- Median decoded wrist temperature, after 30 minutes and 80% recorded coverage.
- Wrist-temperature change relative to at least seven prior nights within 28 days. Only the
  longest logged sleep of at least three hours per prior local date contributes. This establishes a personal baseline;
  it is not temperature calibration and does not turn wrist temperature into core temperature.

Awake minutes affect reported duration only; without timestamped awake intervals, physiological
summaries cover the full logged interval. Sleep physiology is recomputed for recent 35-day
sessions as history arrives. Older journal entries remain stored, but are not reanalyzed by
this job. The public UI shows the latest completed sleep for up to 36 hours.

## API and Grafana

Authenticated routes: `GET /v1/wellness`, `PUT /v1/profile`, `PUT /v1/sleep/{uuid}`.
The single-user profile and journal share the installation's ingestion credential.
The analysis service runs every 30 seconds; results expire after 180 seconds without analysis.
Profile and journal edits invalidate cached results until recomputation. No stale score is
shown while a local edit is pending or the backend is unavailable.

Prometheus scrapes `life_strain_score`, `life_cardio_load`, `life_strain_coverage_ratio`,
`life_sleep_score`, `life_sleep_duration_seconds`, and available `life_sleep_*` physiology.
These are current analysis gauges, separate from original-time raw metrics. A late backfill
updates today's total; past Prometheus scrapes are not rewritten. Missing values are omitted;
readiness gauges remain explicit. Neither deployment nor the app seeds physiological values.

## Research and remaining metrics

[NOOP](https://github.com/ryanbr/noop/tree/67149e46285aff55357cdd0df9631c6c67d52b6f)
was reviewed as research. Its [PolyForm Noncommercial license](https://github.com/ryanbr/noop/blob/67149e46285aff55357cdd0df9631c6c67d52b6f/LICENSE)
is not a permissive open-source license. No NOOP implementation was incorporated in this
scoring change. Life's implementation was written independently around the stated methods.
NOOP's heuristic sleep staging is not a validated substitute for sleep labels. Its candidate
WHOOP fields also differ from Life's firmware-confirmed WHOOP 4 layout.

HRV, respiration, oxygen, and temperature retain their existing documented evidence and
quality gates. In particular, no SpO₂ is fabricated when WHOOP has no usable result.
Respiration remains estimated and independently unvalidated. Calories, steps, automatic
sleep stages and a composite recovery score are not implemented. They need separate data
and validation, not additional fixed cards.
