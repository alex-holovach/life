# Stored optical waveform research

Harvard **41.17.4.0** includes short optical waveform blocks in R25 history.
Life already preserves these packets before acknowledging history. The research
decoder now reconstructs their encoded samples for offline analysis and the
authenticated sensor inventory. They do not produce HRV, breathing or oxygen metrics.

## R25 layout

Frame type `0x2F`, revision `25`, exact length `84`. Offsets are frame-absolute;
all multi-byte fields are little-endian. CRC8 and CRC32 must pass.

| Offset | Encoding | Meaning established by the firmware trace |
| --- | --- | --- |
| 7 | uint32 | Zero-extended **uint16** capture counter; wraps at 65536. |
| 11, 15 | uint32, uint16 | Original block seconds and fraction /32768. The precise first/last-sample time anchor remains unresolved. |
| 17 | uint16 | Capture-group counter, incremented when a new recording cycle starts. |
| 19 | int32 | Initial sample of one optical channel. Wavelength is unconfirmed. |
| 23–70 | 24 × int16 | Successive sample differences, saturated to −32768…32767. |
| 71 | float32 | Motion-related value copied from the block's signal-processing output; retained as a raw candidate. |
| 75, 77, 78 | uint16, uint8, uint8 | Additional sensor/status fields, retained without a health-quality interpretation. |

For an unclipped record, cumulative addition reconstructs 25 samples. A difference
at either saturation boundary is ambiguous: amplitude may have been lost and all
following cumulative values may be wrong. Life preserves the initial value and
differences, but withholds the reconstructed waveform for that record.

The firmware outputs a 40-record ring buffer. Recording and publication are
intermittent; the record counter continues between bursts. Consecutive R25 packets
are therefore not proof of continuous optical recording. At the nominal
100/104-second block cadence, 40 blocks span about 38.5 seconds and 25 samples per
block imply 26 samples/second. This does not establish individual sample timestamps
or make separated bursts suitable for concatenation.

## Executable firmware evidence

The binary hash is pinned in [verify_hrv_firmware.py](../scripts/verify_hrv_firmware.py).
The proprietary binary and personal recordings are not distributed.

```sh
python scripts/verify_optical_firmware.py /path/to/harvard-41.17.4.decompressed.bin
```

This requires Unicorn and executes the original instructions:

- `0x8d26a–0x8d2d4`: initial sample, metadata and saturated differences into the ring.
- `0x80f00–0x80f6a`: ring values into the published structure.
- `0x604e8–0x604f2`, `0x35f8a–0x35f96`: event and collector copies.
- `0x349bc–0x34a3c`: collector values into R25.

Synthetic rising, falling, signed and clipped inputs are passed through these
instructions and then Life's decoder. Tests also verify the counter's 16-bit
truncation, CRC rejection, malformed lengths, saturation handling and inventory
deduplication. Passing means the tested software encoding matches; it is not a
sensor-accuracy or channel-identification result. The generic inventory retains
`research_layout_unverified` because it does not attest the device's firmware.

## Pulse suppression and remaining work

The same verifier executes `0x87c5e–0x87cce` and `0x86862`: the interval-output
criterion is the maximum of 30 block maxima from an internally filtered signal.
A single above-threshold block suppresses output for that call and the next 29
calls, even when subsequent inputs are low. The existing strict `<0.03` gate acts
after peak detection. The per-record motion scalar is a different computation;
substituting it for this internal history would not establish uninterrupted beats.

The R25 counter-width fix prevents false history gaps at rollover. Intentional gaps
between recording groups still need separate transfer-boundary validation; they
must not be accepted by simply disabling missing-record checks.

Next steps are to establish the exact sample clock, identify the channel and
capture a sufficiently long continuous waveform with verified cleanup. Clipping,
group boundaries, missing packets and waveform quality need explicit treatment in
any separately versioned estimator. A single unidentified channel is insufficient
for calibrated red/infrared SpO2. Production HRV and breathing gates are unchanged.
