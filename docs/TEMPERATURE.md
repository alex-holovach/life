# Skin temperature: firmware-derived decoding

Updated September 14, 2026 UTC. The skin-temperature field is identified for
**WHOOP 4 / Harvard 41.17.4.0, R24 history records**. Life decodes it directly:

```text
skin_sensor_celsius = signed_int16_little_endian(frame[76:78]) / 10
```

The offset includes the four-byte transport header; the complete frame is 104 bytes,
with packet type 0x2f and record version 24. Firmware verification, framing checks,
timestamps and invalid-reading handling are required in addition to this formula.

No measured wrist/body temperature, assumed normal temperature, linear fit or private
calibration file is used. The earlier empirical calibration path has been removed.

## Evidence and reproducibility

The strap's archived CRC-valid REPORT_VERSION responses identify Harvard firmware
41.17.4.0. In the observed 84-byte response to command 7, the Harvard version comprises
four little-endian uint32 values starting at absolute byte 10. The known response
prefix at bytes 8–9 is 01 01. Unsupported response schemas are recorded as unknown.

We inspected the matching public firmware archive, not another strap's guessed
conversion. No firmware was flashed and no sensor configuration was changed.

- Archive: [Harvard 41.17.4.0 release](https://github.com/tanarchytan/whoop-firmware/releases/tag/harvard-41.17.4.0).
- ZIP SHA-256: `795ab6dd710e1f7ce58feb8877ead6e171a0a56e1144d997fe2195cb21c7b04d`.
- Decompressed binary SHA-256: `2c4d7a9edf90e4b39bcecda28d2cd9949bd19de533a14d31a7409b31144c309b`.
- Binary load address: `0x10028000`. Addresses below are **file offsets**; add the load address for runtime addresses.

The complete data flow is:

| File offset | Evidence |
| --- | --- |
| `0x2e43a` | Named skin-temperature driver reads the MAX30208's two bytes, swaps/sign-extends the word, multiplies by float 0.005 and caches Celsius at driver +0xf0. Emits raw word in event 0x2a0. |
| `0x2e898` | Signed MAX30208-word-to-Celsius conversion helper, using the same 0.005 constant. |
| `0x574fe` / `0x57562` | Temperature controller's sensor selector 3 dispatches the MAX30208 read request. TMP103 and MAX77818 use separate branches. |
| `0x57620`–`0x5769a` | MAX30208 response calls the conversion helper, places the Celsius float in event +0x0c, and reports sensor selector at +0x10 in event 0x2a6. |
| `0x38880` / `0x389e0` | Collector receives event 0x2a6; selector 3 copies that float into collector +0x31bc. Selector 1 instead copies the TMP103 integer into +0x31b8. |
| `0x346a8` | R24 serializer builds an aa6400a1 header at collector +0x2fc0, establishing the absolute frame base and 104-byte layout. |
| `0x34786`–`0x347a6` | Reads collector +0x31bc, multiplies by 10, calls roundf, converts to signed integer and stores a halfword at +0x300c. Difference 0x300c - 0x2fc0 = **76**. |
| `0x92adc` | Float rounding routine used by the serializer. |
| `0x3221e` / `0x38a2a` | Initialization and error handling set the skin float to **70.0°C**, becoming raw **700**. Life suppresses it. |
| `0x2e88c` / `0x49dac` | Cached skin Celsius accessor also feeds the explicit skin-temperature Celsius diagnostic. |

The primary [MAX30208 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX30208.pdf)
agrees with the firmware's signed digital conversion and 0.005°C sensor resolution.
WHOOP's history serialization reduces that to **0.1°C steps**. This is resolution,
not a claim of complete wearable measurement accuracy.

The earlier MAX6631 teardown reference is not the driver found in this exact firmware.
Likewise, public byte-72 formulas (a fitted linear slope or raw ×0.04) are not used.
The R24 generic temperature at absolute byte 19 comes from the separate TMP103 path.
The nearby words at 68–74 follow another event path; they are not the MAX30208 value.

To independently execute the actual driver conversion and packet packing instructions:

```sh
uv run --with unicorn python scripts/verify_temperature_firmware.py /path/to/harvard-41.17.4.decompressed.bin
```

This script checks the exact binary hash and uses an offline ARM emulator. It executes
the two relevant instruction ranges, including firmware roundf, against ten sensor
inputs, with no device connection. Cases include signed input, rounding boundaries and
the unavailable sentinel. For example, 6740 sensor counts produce approximately
33.7000008°C internally and packet word 337, decoded as 33.7°C. 6750 counts produce
33.75°C and word 338. Binary floating point explains why 6730 counts round to 336.
The firmware itself is not included in the repository.

Regression tests construct synthetic CRC-valid packets with explicit fields and
check the offset and conversion independently of personal recordings.

## Publication and invalid states

- Every complete packet must pass header CRC8, payload CRC32 and exact type/version/length checks.
- Version evidence is stored per device with its original observation timestamp. Decoding
  requires the latest known firmware at the record time to be 41.17.4.0. Data predating
  all version evidence, unknown firmware and v25 records remain raw. New response formats
  stop the decoder instead of inheriting old firmware support.
- Timestamps use original device seconds plus 1/32768-second ticks. Invalid fractional
  ticks and implausible device clocks are rejected; upload time never replaces record time.
- Raw 700 is the firmware's unavailable sentinel. Negative values and values above the
  MAX30208's 0–70°C operating envelope are preserved raw but excluded from Celsius series.
- Missing heart rate does not change the sensor's digital temperature. Contact detection
  and thermal settling are separate questions. A decoded value alone does not establish
  that the strap was being worn or settled against the skin.
- `whoop_skin_temperature_celsius` and `whoop_skin_temperature_valid` carry the bounded
  decoder label `harvard_41_17_4_0_r24_v1`. No per-device serial number enters Prometheus labels.
- The API/app hide values older than ten minutes, unsupported firmware and sensor errors.
  Grafana uses the same validity and freshness rules, plus a current decoder-support gauge.
  The app's one-minute chart means preserve breaks in collection; they are display data.

Life archives all raw captures before acknowledgement. `temperature_reprocess.py`
indexes firmware responses first, then replays history into the durable numeric outbox.
Repeated runs do not duplicate samples. Conflicting values at an existing series/time
raise an error; a revised interpretation must use a new decoder version. Records older
than the configured late-upload window stay archived without being retimestamped.

```sh
docker compose exec -T api /app/.venv/bin/python temperature_reprocess.py
```

No extra runtime service is required. The existing worker uploads the newly decoded
series to Prometheus. `GET /v1/temperature` uses the existing bearer authentication.
The existing raw diagnostics remain available for research. `temperature_reference.py`
can still save a later independent cross-check, but cannot configure or alter conversion.

## Limits

This establishes the on-device skin sensor's transmitted value and units. It does not
claim core body temperature, verify the physical accuracy of this individual strap,
or reproduce WHOOP's overnight averaging/filtering. Baseline deviation still requires
usable overnight data and defined sleep windows. The short observed session does not
justify inventing that baseline. HRV, SpO2 and respiratory-rate validation are separate.

