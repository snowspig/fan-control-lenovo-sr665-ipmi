# Phase 0 probe results — SR665 fan control

- Date: 2026-11-25
- Machine: Lenovo ThinkSystem SR665 V1 (2× EPYC 7532, 8× 990 PRO behind HighPoint SSD7540)
- XCC firmware 3.01, BIOS D8E116E-2.01
- Host state during probe: idle (load 0.13), ambient 25 °C

## 1. The OEM fan command

The command in the upstream repository (`raw 0x3c 0x30 …`, the SR655 V1 form) is
**rejected by this XCC**:

```
raw 0x3c 0x30 0x00 0x01 0x28  ->  rsp=0xc1  Invalid command
raw 0x3c 0x30 0x00 0x00 0x32  ->  rsp=0xc1  Invalid command
raw 0x30 0x30 0x02 0xff 0x32  ->  rsp=0xc1  Invalid command
```

The working form (same shape as the one used on SR665 in the XCP-ng community)
is:

```
raw 0x3a 0x07 0xff <speed byte> <mode>
```

| byte | meaning |
| --- | --- |
| 3rd | fan selector — only `0xff` (all fans) is accepted; `0x01` does not change any fan |
| 4th | speed byte, 1..255 (0 is reserved for "automatic") |
| 5th | mode: `0x01` = fixed speed, `0x00` = hand control back to the BMC |

`raw 0x3a 0x07 0xff 0x00 0x00` returns the fans to the BMC's own policy
(measured 8880–9546 rpm, the nominal reading in the SDR). No BMC reset needed.

Ramp time: the fans take ~15–20 s to settle on a new speed, so a change must
not be read back immediately.

## 2. Speed byte → measured rpm

Lowest measured rotor speed per byte (front / rear):

| byte | front rpm | rear rpm | lowest rotor |
| --- | --- | --- | --- |
| 0x01 | 1520–2240 (unstable) | 1628–2146 | — (not usable) |
| 0x05 | 1280 | 1332 | 1280 |
| 0x08 | 1600 | 1628 | 1600 |
| 0x0a | 1680–2080 | 1776–1924 | 1680 |
| 0x0f | 1920 | 1998 | 1920 |
| 0x14 | 2240 | 2368 | 2240 |
| 0x19 | 2560–2640 | 2738 | 2560 |
| 0x1e | 3040 | 3182 | 3040 |
| 0x28 | 3680–3760 | 3922 | 3680 |
| 0x32 | 4400 | 4662 | 4400 |
| 0x3c | 5040–5200 | 5402 | 5040 |
| 0x4b | 6080–6240 | 6438–6660 | 6080 |
| 0x64 | 8000–8240 | 8510 | 8000 |
| 0x7f | 10000 | 10434 | 10000 |
| 0xc0 | 14080–14960 | 14948–15984 | 14080 |
| 0xff | 18400 | 18426 | 18400 (tach occasionally unavailable) |

- **Hard floor: byte 8** (1600 rpm, comfortably above the BMC's 960 rpm
  lower-critical threshold). Byte 5 (1280 rpm) is below it and byte 1 behaves
  unpredictably.
- The mapping is roughly 80 rpm per byte but is lifted at the low end
  (byte 20 → 2240 rpm rather than 1600), so it is stored as a measured table
  in `deploy/config.toml` (`expected_rpm`) instead of a formula.
- At byte 255 the BMC occasionally reports a tach as unavailable (0xff) even
  though the fan spins; a missing reading is therefore not treated as a fault.

## 3. BMC automatic policy (the noise we are replacing)

At idle (CPU 41 °C, NVMe 39 °C, DIMM 34 °C, ambient 25 °C) the BMC runs the
fans at 8880–9546 rpm, i.e. at the nominal speed from the SDR. That is the
noise floor of the stock configuration.

## 4. Sensor addressing

| sensors | number | LUN | conversion |
| --- | --- | --- | --- |
| Fan 1–6 front tach | 0x41–0x46 | 0 | rpm = 80 × raw |
| Fan 1–6 rear tach | 0x49–0x4e | 0 | rpm = 74 × raw |
| DIMM n temp | 0x50 + n | 1 | °C = raw |

The same sensor number on the wrong LUN returns a *different* sensor (DIMM 16
reads 20 °C on LUN 0 instead of its real 32 °C), which is why `fanctl run`
cross-checks the fast raw path against the SDR path at every start.

## 5. NVMe temperatures

The HighPoint driver claims the drives, so the kernel no longer exposes `nvme`
hwmon chips. Temperatures come from `hptraidconf query devices 1/E1/n`
(~3 s per drive, hptsvr serialises them; 8 drives ≈ 24 s):

```
nvme 1/E1/1: 39C   1/E1/5: 34C
nvme 1/E1/2: 39C   1/E1/6: 33C
nvme 1/E1/3: 39C   1/E1/7: 33C
nvme 1/E1/4: 38C   1/E1/8: 33C
```

`hptraidconf` must be used in single-shot argv form: piping commands into its
interactive mode makes it loop on EOF and flood stdout (it wrote 2.4 GB to
/tmp in 15 minutes during exploration).

## 6. Outcome

- [x] Go — the command works, the floor and the table are measured, and the
      BMC can be handed back at any time with mode `0x00`.
- [ ] No-Go

## 7. Configuration in use

```toml
min_duty = 30          # about 3040 rpm
hard_floor_duty = 8    # about 1600 rpm
safe_duty = 100        # about 8000 rpm
cpu_curve  = [[55, 30], [65, 60], [75, 100], [85, 255]]
dimm_curve = [[70, 30], [78, 60], [85, 255]]
nvme_curve = [[55, 30], [65, 60], [72, 100], [78, 255]]
```

Byte 255 (18.4k rpm) is the emergency speed: as loud as the machine can get,
used only above 85 °C CPU / 85 °C DIMM / 78 °C NVMe.

## 8. Acceptance (2026-11-25, service enabled)

### Idle

After the start-up slew (byte 100 → 30 in about 45 s) all six fans settled at
**2960–3040 rpm front / 3108–3182 rpm rear** (stock BMC policy: 8560–8880 /
9176–9546). Temperatures at idle with that airflow: CPU 46–49 °C, DIMM 36–37 °C,
NVMe 39–41 °C. No warnings in the journal.

### Full CPU load (128 threads, 4 minutes)

| phase | CPU Tctl | duty byte | fan 1 front | DIMM | NVMe max |
| --- | --- | --- | --- | --- | --- |
| start | 65 °C | 61 | 2960 rpm | 38 °C | 42 °C |
| +20 s | 78 °C | 141 | 7040 rpm | 39 °C | 42 °C |
| +40 s to +3.5 min | 81–83 °C | 193–220 | 14080–17120 rpm | 36–38 °C | 41–42 °C |
| load off, +2 min | 48 °C | 30 | 3520 rpm | 37 °C | 41 °C |

- The loop responded within one cycle: duty 30 → 220 in about 40 s.
- No emergency threshold was reached (85 / 85 / 78 °C). DIMM and NVMe stayed
  cool under a pure CPU load.
- 82 °C at 14–17k rpm is what cooling 2×200 W costs in this chassis; the only
  way to make a full load quieter is to cap CPU power (BIOS cTDP/PPT).
- The ramp-down tail is long: with `max_fall_per_cycle = 8` the fans needed
  about two minutes to return to idle while the CPU was already below 55 °C.
  A value of 16 (about one minute) is the recommended tuning.

### Failure injection (2026-11-25, done)

```
23:49:57  SIGKILL to python3 and its ipmitool/hptraidconf children
23:49:57  ExecStopPost: "fan control handed back to the BMC"
23:50:00  restart #1: startup cross-check failed (fans were still ramping)
23:50:10  exit 1 -> ExecStopPost again -> BMC auto
23:50:13  restart #2: cross-check passed, ramped 100 -> 30 by 23:51:04
```

Outcome: the service recovered by itself and the fans returned to ~3000 rpm.
The first restart failed the cross-check because the two reads (~10 s apart)
were taken while the fans were climbing back to the BMC speed; that is a
false positive, not a sensor mapping error. Fixed by retrying the check after
20 s (3 attempts before refusing to start), so a crash transient no longer
aborts a restart.
