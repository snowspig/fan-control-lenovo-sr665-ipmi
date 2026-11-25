# sr665-fanctl — quiet fan control for the Lenovo ThinkSystem SR665

[中文说明](README.md)

A closed-loop IPMI fan controller for the **Lenovo ThinkSystem SR665 (V1)**. The
stock XCC policy keeps all six fans at **~8,900 rpm even when the machine is
idle**; this tool brings that down to **~3,000–4,300 rpm** and ramps the fans up
again as soon as the CPU, DIMMs or NVMe drives get warm. Every failure mode ends
up *louder*, never quieter.

- Python standard library only (3.11+), no third-party dependencies
- Single fan zone (the six fans share one N+1 airflow path, so per-fan control
  buys nothing)
- Sensor reads are cross-checked against the SDR reference at every start; a
  mismatch refuses to start
- Hands fan control back to the BMC when the service stops or crashes
  (systemd `ExecStopPost`)

> **⚠️ Read this first.** Lenovo states that manual fan commands are
> "not supported/recommended" and "could cause thermal damage to internal
> components" (see [References](#references)). This project is verified on one
> SR665 V1 and is **not responsible for your hardware**. Do the
> [Phase 0](#first-use-phase-0-is-mandatory) calibration before enabling it.

## Why

The server lives at home. With zero load, the CPU at 41 °C and 25 °C ambient,
the XCC still runs the six Performance dual-rotor fans at 8,560–8,880 rpm — the
"nominal" speed from the SDR. The noise is BMC policy, not a cooling need.

The existing script for this family, [`fyr233/fan-control-sr655`](https://github.com/fyr233/fan-control-sr655),
targets an SR655 V1 **with AMD GPUs**: its fan mapping follows GPU temperatures,
its OEM command (`raw 0x3c 0x30 …`) is rejected by the SR665's XCC
(`rsp=0xc1`), it never reads the tach back, and the repository has no license.
The XCC web interface has no fan setting at all. Hence this rewrite, calibrated
on real hardware ([docs/probe-results.md](docs/probe-results.md)).

## How it works

```text
  ┌──────────────── every 5 s ────────────────┐
  │ sysfs hwmon:  CPU1/CPU2 Tctl (k10temp)      │
  │ raw IPMI:     12 fan rotors, 4 DIMMs       │
  │               (one message each, ~0.08 s)  │
  │ background:   NVMe ×8 temps (hptraidconf)  │
  └───────────────────┬────────────────────────┘
                      ▼
   CPU / DIMM / NVMe curves → max → 2 °C hysteresis → slew-limited fall
                      ▼
   ipmitool raw 0x3a 0x07 0xff <speed byte> 0x01
                      ▼
   read the 12 rotor speeds back ──► verify the write took effect
```

| Rule | Value |
| --- | --- |
| Speed byte 0 is never sent | it is the "back to BMC automatic" mode |
| `hard_floor_duty` | nothing may go below it (measured byte 8 ≈ 1,600 rpm) |
| Emergency full speed (byte 255 ≈ 18,400 rpm) | CPU ≥ 85 °C / DIMM ≥ 85 °C / NVMe ≥ 78 °C |
| Sensors unreadable for 3 cycles | full speed |
| Rotor below half the expected rpm for 2 cycles | full speed + ERROR |
| IPMI write fails | retry once, then exit and let systemd restart |

## Measured on this SR665 V1

```bash
ipmitool raw 0x3a 0x07 0xff <speed byte> 0x01   # fixed speed, all fans
ipmitool raw 0x3a 0x07 0xff 0x00 0x00           # hand control back to the BMC
```

The byte is **not** a percentage of maximum rpm — the low end is lifted:

| byte | 8 | 15 | 20 | 30 | 50 | 100 | 127 | 192 | 255 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| lowest rotor rpm | 1600 | 1920 | 2240 | 3040 | 4400 | 8000 | 10000 | 14080 | 18400 |

| Scenario | Stock XCC policy | This controller |
| --- | --- | --- |
| Idle, cold chassis (CPU 47–49 °C) | 8,560–8,880 rpm | **2,960–3,040 rpm**, NVMe 39–41 °C |
| Idle, thermally settled (CPU 58–60 °C) | same | **3,700–4,300 rpm** (the curve trims it) |
| 128-thread full load | — | CPU steady at 81–83 °C with 14,000–17,000 rpm; DIMM and NVMe stay cool |
| Crash (SIGKILL) | — | control back to the BMC at once, restart in 3 s, idle again in ~1 min |

Cooling 2×200 W simply needs that airflow; the only way to make a full load
quieter is to cap CPU power (BIOS cTDP/PPT).

## Install

Developed and verified on **Ubuntu 24.04** (kernel 6.8, `ipmitool` 1.8.19,
Python 3.12).

```bash
sudo apt install -y ipmitool

git clone https://github.com/snowspig/fan-control-lenovo-sr665-ipmi.git /tmp/fan-control
sudo cp -a /tmp/fan-control /opt/fan-control
sudo chown -R "$USER":"$USER" /opt/fan-control
rm -rf /tmp/fan-control

sudo bash /opt/fan-control/deploy/install.sh     # idempotent: user, group, udev, unit
sudo usermod -aG ipmi "$USER"                    # new login sessions get the group
```

Installing does **not** enable the service; Phase 0 comes first. The service runs
as a dedicated `fanctl` user in the `ipmi` group — a udev rule grants
`/dev/ipmi0` to that group. No root, no sudoers entry.

## First use: Phase 0 is mandatory

Sensor numbers, LUNs, conversion factors and the byte scale are
**firmware-specific**. The values in this repository are only known to hold for
one SR665 V1 with XCC 3.01. Re-calibrate on any other machine, and after every
XCC firmware update. While the machine is idle (wait ~20 s after each change for
the fans to settle):

```bash
fanctl verify        # raw reads must agree with the SDR reference
fanctl status        # read-only: temperatures and fan speeds
fanctl set 60 && sleep 20 && fanctl status
fanctl set 40 && sleep 20 && fanctl status
fanctl set 20 && sleep 20 && fanctl status     # keep going until a rotor < 1500 rpm
# the lowest byte with both rotors >= 1500 rpm is hard_floor_duty
fanctl auto          # back to the BMC (works any time; also the rollback)
```

Put `hard_floor_duty`, `expected_rpm` (byte→rpm table) and `min_duty` (idle
target) into `/etc/fanctl/config.toml`, then:

```bash
sudo systemctl enable --now sr665-fanctl
journalctl -u sr665-fanctl -f     # expect: duty 100 -> 84 -> ... -> 30
```

## Configuration

`/etc/fanctl/config.toml` (template: [deploy/config.toml](deploy/config.toml);
restart the service after editing).

| Key | This machine | Meaning |
| --- | --- | --- |
| `min_duty` | 30 | idle target (≈3,040 rpm) — the quiet/airflow trade-off |
| `hard_floor_duty` | 8 | hardware floor (≈1,600 rpm), from Phase 0 |
| `safe_duty` | 100 | start value (≈8,000 rpm) |
| `cpu_curve` | 55/65/75/85 °C → 30/60/100/255 | CPU Tctl curve |
| `dimm_curve` | 70/78/85 °C → 30/60/255 | DIMM curve (JEDEC limit 85 °C) |
| `nvme_curve` | 55/65/72/78 °C → 30/60/100/255 | NVMe curve (990 PRO crit ≈ 85 °C) |
| `max_fall_per_cycle` | 16 | at most 16 bytes down per 5 s (rises are immediate) |
| `emergency_c` | cpu 85 / dimm 85 / nvme 78 | thresholds for byte 255 |
| `[tach]` / `[dimm]` / `[nvme]` | see template | sensor numbers, LUNs, conversion factors |

Tuning tip: when the idle CPU temperature sits right at the first curve knee,
the controller trims between two bytes (here roughly every 3–5 minutes between
bytes 38 and 44, about 400 rpm). For a quieter idle raise the first knee (for
example 55 → 62 °C) at the cost of a warmer idle.

## Commands

| Command | Effect |
| --- | --- |
| `fanctl status` | temperatures, six fan speeds, the byte each curve suggests (read-only) |
| `fanctl status --nvme` | also query the NVMe SMART temperatures (~3 s per drive) |
| `fanctl set <byte>` | fixed speed byte, clamped to `hard_floor_duty` |
| `fanctl auto` | hand control back to the BMC (idempotent; the rollback path) |
| `fanctl verify` | cross-check the sensor paths (also done by `run` at start) |
| `fanctl run --dry-run --once` | print the command that would be sent |
| `fanctl run` | the loop itself (what systemd runs) |

## Safety design

- **One failure direction.** Sensors gone, command refused, process crashed — the
  fans always end up faster, never slower.
- **Start-up gate.** The slow path (`ipmitool sensor list`, ~8 s) provides an
  authoritative reading which every fast raw reading is compared against (fan
  tolerance max(200 rpm, 5 %), temperature 3 °C). A mismatch is retried twice
  after 20 s (fans ramp for 15–20 s), then the service refuses to start: reading
  the wrong LUN silently returns *another sensor's* value (measured: up to
  −40 °C for a DIMM that is at 31 °C).
- **Tach readback.** Each cycle verifies the previous write against the measured
  rotor speeds; a rotor that really stopped reports 0 and is caught. At the top
  speed the BMC occasionally reports a tach as unavailable — a missing reading is
  not a fault.
- **Back to the BMC is the safe resting state**: its own policy (~8.9k rpm) is
  more conservative than any manual value, and `ExecStopPost` restores it.
- Least privilege: `fanctl` user, `ipmi` group, udev rule, hardened systemd unit.

## Known limitations

- **NVMe temperatures lag by ~30 s.** With the HighPoint driver the kernel no
  longer exposes NVMe hwmon chips, so temperatures come from `hptsvr` through
  `hptraidconf` — about 3 s per drive, serialised. A background thread refreshes
  one drive at a time (8 drives ≈ 24–32 s per round); readings older than 90 s
  are discarded.
- **The HighPoint card, the MegaRAID card and the HDDs are not in the loop.**
  Fine for a CPU-only machine; judge for yourself if yours has GPUs or a full
  set of drives.
- **If the kernel hangs, the controller stops too.** The fans stay at the last
  byte. An IPMI watchdog is the usual mitigation (not part of this project).
- **Re-run Phase 0 after any XCC firmware update.**

## Pitfalls (all learned the hard way)

1. **Never feed commands to `hptraidconf` on stdin.** Its interactive mode loops
   forever on EOF and floods stdout (2.4 GB in 15 minutes). Single-shot argv
   form only, with a hard timeout and an output size limit.
2. **`ipmitool sensor list` takes ~8 s** (it walks ~300 SDR records). The loop
   uses raw `Get Sensor Reading` instead: `ipmitool -l <lun> raw 0x04 0x2d <no>`,
   one message, ~0.08 s.
3. **The same sensor number on another LUN is a different sensor.** DIMMs live on
   LUN 1 here. A sensor's identity is (number, LUN) — hence the cross-check.
4. **Fans need 15–20 s to settle.** Reading the speed right after a write gives a
   value in between; any check that compares two reads must wait for a steady
   state.
5. **The speed byte is not a percentage of maximum rpm.** It is a request value
   (0–255) whose resulting rpm depends on the fan. Here rpm ≈ 1000 + 68 × byte.
6. **OEM fan commands are model-specific.** The SR655 V1's `0x3c 0x30` returns
   `0xc1 Invalid command` on the SR665 V1. Always measure on your own machine.

## References

Each source below was checked against its original text:

- **Lenovo ThinkSystem SR665 product guide** (Lenovo Press,
  [lp1269.pdf](https://lenovopress.lenovo.com/lp1269.pdf)) — "up to six
  hot-swap redundant fans"; "Standard fans (single-rotor 17K RPM) or Performance
  fans (dual-rotor 18K RPM)" (the 18,400 rpm maximum measured here matches the
  dual-rotor Performance fans).
- **XCP-ng forum: "High Fan Speed Issue on Lenovo ThinkSystem Servers"**
  ([page 5](https://xcp-ng.org/forum/topic/8548/high-fan-speed-issue-on-lenovo-thinksystem-servers/55?page=5))
  — the public origin of `raw 0x3a 0x07 0xff <byte> 0x01` (posted by user Riven
  from information obtained from Lenovo, demonstrated on an SR635 **V3**), and
  the quoted Lenovo warning: "NOT SUPPORTED/RECOMMENDED - and could cause
  thermal damage to internal components". This repository adds: verification on
  an **SR665 V1** (XCC 3.01), the byte→rpm calibration table, and the finding
  that mode byte `0x00` hands control back to the BMC directly (the thread
  assumed an XCC restart was needed).
- **[fyr233/fan-control-sr655](https://github.com/fyr233/fan-control-sr655)** —
  the idea (SR655 V1 with GPUs). This is a rewrite for the SR665 with a different
  command, sensor layout and safety model.

## License

[MIT](LICENSE). Not affiliated with, supported or endorsed by Lenovo.
