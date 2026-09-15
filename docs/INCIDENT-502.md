# Dashboard and ingestion availability

An intentional API outage and an earlier stop-and-copy backup interrupted the
service. The gateway could not reach stopped upstream containers. Those incidents
did not establish spontaneous container failures.

The old `./life backup` stopped every running Life service for a consistent filesystem copy.
It now snapshots SQLite through the backup API and Prometheus through its private snapshot
API, without service stops. The ingestion snapshot precedes the Prometheus snapshot; its
immutable referenced archives are included. Supplemental TSDB blocks from sent numeric
records cover Prometheus 3.14's snapshot omission of recent out-of-order head samples.

A continuous API/Grafana probe fails against the old backup. The replacement passes in an
isolated stack, and a full restore verifies ordinary and delayed samples at their original
timestamps. A subsequent deployed backup also completed with successful health probes. Container restart policy and health checks remain enabled; future fault injection
belongs only in the isolated acceptance stack. Routine backup no longer restarts services.

Sources for the backup implementation:
- https://www.sqlite.org/backup.html
- https://github.com/prometheus/prometheus/blob/v3.14.0/tsdb/db.go#L2544

The production ingestion archive retained queued phone records across the earlier outage.
One pre-existing conflicting sample remains recorded for review; it was not discarded or
retimestamped as part of this fix. Overnight capture and whole-PC reboot remain separate
acceptance tests.
