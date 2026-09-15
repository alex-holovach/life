# iOS regression checks

## Stop survives relaunch while Bluetooth is unavailable

Use the iPhone simulator, where CoreBluetooth is unavailable, to exercise the same
app lifecycle and UI state used on the phone. No automated XCUITest target exists yet.

1. Open Life → Settings → Stop collecting.
2. Verify the state says Stopped and the Resume collecting button appears.
3. Terminate and relaunch the app, preserving its data.
4. Verify the main screen still says Stopped.
5. Open Settings and verify Resume collecting remains available.
6. Resume. Verify the action clears Stop intent and reports Bluetooth unavailable.
7. Relaunch again and verify Settings offers Stop collecting.

Initial failure observed on iOS 26.5: step 4 showed Bluetooth unavailable, and Settings
offered Stop collecting again despite the persisted stop flag. The Bluetooth-state callback
overwrote the stopped status, and the view inferred intent from that display string.

The fix keeps collection intent as an observable property and prioritizes it when handling
Bluetooth state changes. This check covers intent and UI state; it does not validate hardware
reconnection or Bluetooth restoration.

Result after the fix: all seven steps pass on the iPhone 17 Pro simulator, iOS 26.5,
on 2026-09-13. Reinstalling the updated app preserved the previously stopped state;
Resume also remained effective after another termination and relaunch.


## Real-device notification framing and export status

The first iPhone session on 2026-09-13 exposed two regressions:

- After checking an empty queue, a new BLE capture arrived inside the upload batch delay.
  The count grew but the status remained “All captured data saved on PC.” The uploader now
  renders idle status from the current queue and last acknowledgement. The test imports the
  actual uploader, uses isolated defaults and an ephemeral session, checks the empty queue,
  inserts a capture, and calls sync during the delay. It failed before the fix and passes after it.
- Channel `61080007…` notifications use a different envelope from the CRC-framed command,
  event and data channels. Feeding them into the frame parser produced a false warning.
  The notification decoder now routes only `…0003/0004/0005` into independent reassemblers;
  other channel payloads are still archived raw. The regression interleaves an opaque notification
  with fragmented/coalesced frames and verifies that real corruption still raises a warning.

Replay through the fixed Swift decoder: 164 original iPhone captures, 72 CRC-valid frames,
zero discarded framing bytes. The updated physical iPhone screen shows queued records as
“Waiting to upload to PC,” a recent PC acknowledgement, and no false framing warning.
All 164 captures from the initial snapshot were still present after reinstalling.

## Battery queries recover after Bluetooth restoration

1. Collect HR and a cross-checked battery reading on the physical iPhone.
2. Reinstall or terminate/relaunch Life without deleting its data.
3. Inspect the new session's raw records for command replies and fresh standard/custom battery reads.
4. Verify new battery_verified records and PC acknowledgements.

Before the fix, a restored session received live HR and notifications but no command responses
or custom battery query for more than 40 seconds. Service discovery ran in willRestoreState,
before Bluetooth reported powered on. Deferring setup to the powered-on callback restored the
command/battery flow. A second terminate/relaunch repeated the passing result, with fresh matching
standard/custom battery reads within seconds. Stopped restoration also defers cancellation until
Bluetooth is ready. No automated CoreBluetooth hardware simulator exists here; this is a
recorded physical-device lifecycle regression, not an automated unit-test claim.

References: [Apple Bluetooth state rules](https://developer.apple.com/documentation/corebluetooth/cbcentralmanagerdelegate/centralmanagerdidupdatestate(_:)),
[Apple restoration lifecycle](https://developer.apple.com/library/archive/documentation/NetworkingInternetWeb/Conceptual/CoreBluetooth_concepts/CoreBluetoothBackgroundProcessingForIOSApps/PerformingTasksWhileYourAppIsInTheBackground.html),
[WHOOP channel research](https://github.com/ryanbr/noop/blob/main/docs/BLE_REVERSE_ENGINEERING.md).


## Screen-off capture and ingestion interruption

An intentional ingestion outage confirmed that the phone retained queued records
and the backend subsequently received their original IDs. Foreground/manual upload
attempts occurred, so this was not a test of fully unattended retry timing.

Short locked-phone checks observed continued live collection, uploads and periodic
battery queries. USB was attached; longer unplugged endurance remains a separate
check. Eligible numeric records were compared with Prometheus at their original
timestamps. Conflicting records remain archived rather than silently overwritten.
Personal readings, event times and backup identifiers are kept outside the repository.
