# Connection and history recovery
## Behavior

Life keeps a CoreBluetooth connection request pending when the WHOOP is out of range.
It restores its central manager during application launch, including launches without a
visible SwiftUI view. Bluetooth power changes, failed connections, invalidated services,
required subscription failures, and failed writes have explicit recovery paths.

The session supervisor uses monotonic deadlines: 30 seconds for setup, 15 seconds for a
write callback, and 20 seconds without a parsed standard heart-rate notification. It
first restores the HR subscription and stream; another 15 seconds of silence reconnects.
A zero-BPM/off-wrist notification still proves that the stream is running. Battery,
diagnostic and historical packets do not disguise a silent live-HR subscription.
Optional diagnostic subscriptions cannot block the required response/data/HR channels.

A foreground timer supplements checks on Bluetooth callbacks and foreground entry.
This is not a promise that an iOS timer runs while suspended. CoreBluetooth owns pending
connections and wakes the app for supported Bluetooth events. State restoration is set up
before any UI appears. User Stop remains persistent and suppresses recovery.

After reconnecting, two close live device timestamps can confirm the clock. Buffered
live packets cannot authorize a correction. Life also reads GET_CLOCK with the echoed
request sequence; two responses must arrive within two seconds, agree on the offset,
and show a mismatch outside the entire request/response interval before correction.
A phone wall-clock jump invalidates this evidence. The verified nine-byte SET_CLOCK
payload is constructed at the actual write. Subsequent live or matched clock responses
verify the new time. Failed history sessions never trigger a clock rewrite, and old
records are not retimestamped.

Stored history is downloaded after connection and every five minutes while telemetry
arrives. The history state machine archives each original notification and complete
CRC-valid frame, decodes supported v24 records, and commits SQLite writes with FULL
synchronization before returning an acknowledgement. Acknowledgement echoes the observed
eight-byte end-marker token and advances the strap's persistent cursor. No trim/erase or
read-pointer command is sent. Record discontinuity, unknown layouts, checksum/framing
errors or a storage failure prevent acknowledgement. Duplicate acknowledgements are
rate-limited, and idle sessions time out. Packet gaps and idle/ACK timeouts retry on a fresh connection with backoff. Unknown
layouts, malformed markers and rejected ACKs remain paused for review.

Downloaded heart-rate records retain their measurement time, including the 1/32768-second
fraction. This prevents distinct readings within one second from colliding in Prometheus.
The mobile chart uses history to fill gaps and prefers live readings where both overlap.
Its status reports history progress separately from uploading. Connection/recovery events
are durably archived; locally generated diagnostics do not count as fresh strap traffic
in the backend stale-data check.

R25 carries a zero-extended 16-bit counter, unlike R24's 32-bit counter. A rollover
from 65535 to zero now passes continuity checks; a skipped value still blocks ACK.
Unexpected nonzero high bits remain an unsupported layout. This fixes a reproduced
counter-boundary failure, not every reason for a dropped connection. The
[firmware optical verifier](OPTICAL.md) executes the original counter truncation.

## Device checks

Short device tests observed history advancing only after durable storage, recovery
of a deliberate disconnect, and restoration of a disabled HR notification stream.
These establish the tested behaviors, not long-term or physiological accuracy.
Personal event timelines and recordings remain outside the repository.

The debug-only `--reliability-test` launch disables HR notifications, then creates a
90-second disconnection and snapshots the database after five minutes.
`--notification-recovery-test` runs only the subscription test and snapshots after one
minute. These modes generate no fake samples and never run on a normal launch.
Copying a live database and WAL separately is not a consistent diagnostic snapshot.

## Software and deployment checks

- 18 Swift tests pass, including capture durability, standalone SQLite snapshots,
  history commit-before-ACK, missing records, malformed markers, ACK retries, timeouts,
  chart backfill and connection deadlines. CoreChecks passes.
- 21 backend tests pass; one optional external-Prometheus test is skipped. Device replay checks also exercised a deployed Prometheus instance.
- Signed iPhone and unsigned simulator builds succeed. The iPhone build is installed.
- Backend API/worker updated through a temporary healthy API and graceful gateway reloads.
  64 API and 64 Grafana health checks all returned 200 during deployment. Grafana and
  Prometheus containers were not restarted. All five services are healthy.

## Limits and remaining validation

HRV/strain/sleep calculations and additional sensor calibration are separate work. Raw
interval words, accelerometer and optical/temperature candidate fields remain archived;
they are not silently promoted to validated measurements.

Records outside the configured 30-day Prometheus late-upload window are
retained for historical import, with their original timestamps. The decoder includes subsecond timing to avoid collisions caused by second-resolution timestamps.

A short fault test does not validate overnight behavior, a physical out-of-range cycle,
Bluetooth disable/enable, phone reboot/first unlock, or recovery after the user force-quits
Life. iOS cannot promise an uninterrupted radio connection or unlimited background runtime.
Keep the independent storage paths: strap flash, phone SQLite and remote archive. Readings the strap did not store cannot be reconstructed.

Primary references:
- [Apple: Core Bluetooth background processing and state restoration](https://developer.apple.com/library/archive/documentation/NetworkingInternetWeb/Conceptual/CoreBluetooth_concepts/CoreBluetoothBackgroundProcessingForIOSApps/PerformingTasksWhileYourAppIsInTheBackground.html)
- [OpenStrap: WHOOP 4 protocol and history continuation](https://github.com/OpenStrap/research/blob/c6be09c4021546e4693dcf74da25d7b793ff8a40/PROTOCOL.md)
- [NOOP research: firmware-specific clock payload](https://github.com/0xmts/noop-latest/blob/63f3ed06cc2ff1c3d1e2d0e6828bcfe7387ae521/docs/BLE_REVERSE_ENGINEERING.md)

Temperature follow-up: the strap later interleaved v25 records with independent counters.
The transfer now archives the observed 84-byte v25 form without interpreting its sensor
fields. A real 682-record mixed transfer passed. See [TEMPERATURE.md](TEMPERATURE.md).

## Long-running transfer recovery

A dropped history packet used to pause downloads until the next unrelated disconnect,
although live heart rate continued. The ten-minute total transfer deadline also
interrupted healthy backfill; immediately restarting on the same connection let late
records arrive before the new batch marker and leave history blocked.

Transfers now time out only after 30 seconds without history activity. Recoverable
packet gaps, framing failures and idle/ACK timeouts retry on a fresh connection, with
5–300 second exponential backoff. The interrupted batch is never acknowledged. Its
original bytes remain saved and the strap retransmits from the last acknowledged
cursor. Unknown layouts, malformed markers and rejected ACKs still require review.
A completed transfer or a newly recovered timestamp beyond prior progress resets
backoff. Replayed records do not reset it. This avoids escalating to five-minute
pauses despite successful forward progress during a large backlog. BLE callbacks
drive retries in the background.

`history_gap` events retain the record version and expected/received counters.
`ble_processing_slow` records callbacks taking over 100 ms, at most once per minute,
to distinguish local processing stalls from transport failures. These are operational
diagnostics, not physiological measurements.

Each custom notification's original fragment, complete wire frames and decoded
history records are saved in one SQLite transaction with WAL and `synchronous=FULL`.
Only after that commit succeeds can the history state machine produce an ACK.
Encoding or storage failure rolls the whole notification back and stops collection
without advancing the strap's cursor. The uploader is notified once per committed
notification instead of once per row. This reduces write amplification during
backfill while retaining every original byte and the existing replay identities.
Chart refreshes use a separate read-only SQLite connection on a serial worker
queue, so fetching and decoding saved history does not block Bluetooth callbacks.
Only one chart read runs at a time; live points arriving during the read are kept
when its snapshot is applied. `chart_read_slow` records reads exceeding 100 ms,
at most once per minute, independently of the Bluetooth callback timing.
The chart query projects only identity, source, quality, timestamps and HR. It
does not decode full captures or their raw packets and sensor arrays. Both the
live and stored paths share the same eligibility and timestamp rules. A synthetic
one-hour replay verifies identical readings before and after this optimization;
raw storage and upload acknowledgments are unchanged.

The app reports delayed stored history separately from pulse quality. Grafana's
**Stored history delay** shows the latest indexed measurement age; **Analysis queue**
shows pending HRV and breathing calculations. Live heart rate alone does not establish
that the detailed history has caught up.
