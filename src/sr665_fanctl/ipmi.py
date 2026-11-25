"""Talk to the BMC through ``ipmitool``: the only module that changes fan state.

Readings are taken with raw ``Get Sensor Reading`` calls (one IPMI message
each, ~0.08 s) instead of ``sensor list`` (~8 s, it walks the whole SDR
repository). That is only safe because the sensor numbers, LUNs and linear
conversion factors below were measured on the SR665 V1 this was developed
on (XCC 3.01); ``read_reference``
runs the slow SDR-based path so the fast path can be cross-checked.

Measured on that SR665 V1 (XCC 3.01), 2026-11-25:

| sensors | number | LUN | value = m * raw |
| --- | --- | --- | --- |
| Fan 1-6 front tach | 0x41..0x46 | 0 | 80 * raw |
| Fan 1-6 rear tach | 0x49..0x4e | 0 | 74 * raw |
| DIMM n temp | 0x50 + n | 1 | 1 * raw (deg C) |

Reading the same sensor number on the wrong LUN returns a *different*
sensor, so the LUN is part of the sensor identity.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sr665_fanctl.config import DimmSpec, TachSpec

log = logging.getLogger(__name__)

IPMITOOL = "ipmitool"
#: One raw sensor read or one OEM write: a single IPMI message.
TIMEOUT_S = 10
#: ``sensor list`` walks ~300 SDR records; measured at 7.7 s when idle.
REFERENCE_TIMEOUT_S = 30
#: Lenovo OEM fan control. The command form was first published on the XCP-ng
#: forum (demonstrated on an SR635 V3) and is verified and calibrated here on
#: an SR665 V1 (XCC 3.01), see docs/probe-results.md:
#: https://xcp-ng.org/forum/topic/8548/high-fan-speed-issue-on-lenovo-thinksystem-servers/55?page=5
#:
#:   raw 0x3a 0x07 0xff <speed byte> <mode>
#: The speed byte is 1..255 and maps to roughly 80 rpm per unit (calibration in
#: docs/probe-results.md); mode 0x01 = fixed speed, mode 0x00 = hand control
#: back to the BMC. The selector byte is always 0xff: this XCC only accepts
#: the "all fans" form (0x3c 0x30 and 0x30 0x30 are rejected with 0xc1).
RAW_SET_SPEED = ("raw", "0x3a", "0x07", "0xff")
MODE_FIXED = "0x01"
MODE_AUTO = "0x00"
#: Speed bytes below this are not usable: 0x00 means "automatic", 0x01 behaves
#: oddly and 0x05 already drops the rotors to ~1280 rpm (below the BMC's
#: lower-critical threshold of 960 rpm plus margin).
MIN_SPEED_BYTE = 8
MAX_SPEED_BYTE = 255
#: NetFn 0x04 (Sensor/Event), command 0x2d = Get Sensor Reading.
RAW_READ_SENSOR = ("raw", "0x04", "0x2d")
READ_SENSORS = ("sensor", "list")

Runner = Callable[[list[str], float], str]

_FAN_RE = re.compile(r"^Fan (\d+) (Front|Rear) Tach$")
_DIMM_RE = re.compile(r"^DIMM (\d+) Temp$")


class IpmiError(RuntimeError):
    """ipmitool is missing, timed out, or exited non-zero."""


@dataclass(frozen=True)
class IpmiSensors:
    """What only the BMC can see: fan speeds and DIMM temperatures."""

    tach: dict[int, tuple[int, int]]  # fan id -> (front rpm, rear rpm)
    dimm_temps: dict[int, float]  # slot -> degrees C


def build_set_duty_args(duty: int) -> list[str]:
    """The argv of one OEM speed write for all fans.

    ``duty`` is the speed byte (1..255). Byte 0 is never sent: it is how the
    BMC is asked to resume automatic control.
    """
    if not MIN_SPEED_BYTE <= duty <= MAX_SPEED_BYTE:
        raise ValueError(
            f"duty {duty} outside {MIN_SPEED_BYTE}..{MAX_SPEED_BYTE} "
            "(0 is reserved for automatic control)"
        )
    return [IPMITOOL, *RAW_SET_SPEED, f"0x{duty:02x}", MODE_FIXED]


def build_auto_args() -> list[str]:
    """The argv that hands fan control back to the BMC."""
    return [IPMITOOL, *RAW_SET_SPEED, "0x00", MODE_AUTO]


def build_read_args(number: int, lun: int) -> list[str]:
    """The argv of one raw Get Sensor Reading, addressed to ``lun``."""
    if not 0 <= number <= 0xFF:
        raise ValueError(f"sensor number {number} outside 0x00..0xff")
    if not 0 <= lun <= 3:
        raise ValueError(f"lun {lun} outside 0..3")
    return [IPMITOOL, "-l", str(lun), *RAW_READ_SENSOR, f"0x{number:02x}"]


def parse_raw_reading(text: str) -> int | None:
    """The reading byte of a raw Get Sensor Reading response, or None.

    Response: ``reading flags ...``. ``0xff`` means no reading; bit 0x20 of the
    flags means "reading unavailable"; bit 0x40 must be set ("sensor scanning
    enabled"), otherwise the byte is stale.
    """
    tokens = text.split()
    if len(tokens) < 2:
        return None
    try:
        reading = int(tokens[0], 16)
        flags = int(tokens[1], 16)
    except ValueError:
        return None
    if reading == 0xFF or flags & 0x20 or not flags & 0x40:
        return None
    return reading


def parse_sensor_list(text: str) -> IpmiSensors:
    """Parse decoded ``ipmitool sensor list`` output into tach and DIMM temps.

    Used only for the startup cross-check of the fast raw path. Empty DIMM
    slots report ``na`` and are skipped.
    """
    tach: dict[int, list[int | None]] = {}
    dimm_temps: dict[int, float] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 2:
            continue
        name, reading = cells[0], cells[1]
        fan = _FAN_RE.match(name)
        if fan is not None:
            _add_tach(tach, fan, reading)
            continue
        dimm = _DIMM_RE.match(name)
        if dimm is not None and reading not in ("na", ""):
            try:
                dimm_temps[int(dimm.group(1))] = float(reading)
            except ValueError:
                continue
    complete = {
        fan: (speeds[0], speeds[1])
        for fan, speeds in sorted(tach.items())
        if speeds[0] is not None and speeds[1] is not None
    }
    return IpmiSensors(tach=complete, dimm_temps=dict(sorted(dimm_temps.items())))


def _add_tach(tach: dict[int, list[int | None]], match: re.Match[str], reading: str) -> None:
    """Record one rotor reading; a fan is complete once both rotors are seen."""
    if reading in ("na", ""):
        return
    try:
        rpm = int(float(reading))
    except ValueError:
        return
    slot = int(match.group(1))
    index = 0 if match.group(2) == "Front" else 1
    tach.setdefault(slot, [None, None])[index] = rpm


class IpmiClient:
    """Runs ipmitool as a subprocess; ``runner`` is injectable for tests."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner if runner is not None else _run

    def set_duty(self, duty: int) -> None:
        """Send one speed write for all fans; raises IpmiError on refusal."""
        self._runner(build_set_duty_args(duty), TIMEOUT_S)

    def set_auto(self) -> None:
        """Hand fan control back to the BMC (verified in Phase 0)."""
        self._runner(build_auto_args(), TIMEOUT_S)

    def read_sensor(self, number: int, lun: int) -> int | None:
        """One raw reading as a plain integer; None when unavailable or refused."""
        try:
            return parse_raw_reading(self._runner(build_read_args(number, lun), TIMEOUT_S))
        except IpmiError as error:
            log.debug("sensor 0x%02x at lun %d unreadable: %s", number, lun, error)
            return None

    def read_reference(self) -> IpmiSensors:
        """Decoded readings straight from the SDR repository (slow, for checks)."""
        return parse_sensor_list(self._runner([IPMITOOL, *READ_SENSORS], REFERENCE_TIMEOUT_S))

    def discover_dimm_slots(self, spec: DimmSpec) -> list[int]:
        """Probe every possible DIMM slot once and keep the ones that answer."""
        slots = [
            slot
            for slot in range(1, spec.slot_count + 1)
            if self.read_sensor(spec.base_number + slot, spec.lun) is not None
        ]
        log.info("dimm slots with readings: %s", slots)
        return slots

    def read_sensors(
        self, tach: TachSpec, dimm: DimmSpec, dimm_slots: Sequence[int]
    ) -> IpmiSensors:
        """Read the fans and DIMMs with one message each and apply the m factor.

        A single unavailable sensor is left out (the controller decides what
        that means); every read failing means the BMC itself is unusable.
        """
        failures = 0
        tach_map: dict[int, tuple[int, int]] = {}
        dimm_temps: dict[int, float] = {}

        for fan, (front, rear) in enumerate(
            zip(tach.front_numbers, tach.rear_numbers), start=1
        ):
            front_rpm = self._scaled(front, tach.lun, tach.front_m, tach.b)
            rear_rpm = self._scaled(rear, tach.lun, tach.rear_m, tach.b)
            failures += (front_rpm is None) + (rear_rpm is None)
            if front_rpm is not None and rear_rpm is not None:
                tach_map[fan] = (front_rpm, rear_rpm)
        for slot in dimm_slots:
            value = self._scaled(dimm.base_number + slot, dimm.lun, dimm.m, dimm.b)
            failures += value is None
            if value is not None:
                dimm_temps[slot] = value

        total = 2 * len(tach.front_numbers) + len(dimm_slots)
        if total and failures == total:
            raise IpmiError(f"all {total} sensor reads failed")
        return IpmiSensors(tach=tach_map, dimm_temps=dimm_temps)

    def _scaled(self, number: int, lun: int, m: float, b: float) -> int | None:
        """One reading converted to engineering units, or None."""
        raw = self.read_sensor(number, lun)
        return None if raw is None else int(round(m * raw + b))


class DryRunClient(IpmiClient):
    """Prints writes instead of touching the BMC; reads go through ``reader``."""

    def __init__(self, reader: IpmiClient | None = None) -> None:
        super().__init__()
        self._reader = reader if reader is not None else self

    def set_duty(self, duty: int) -> None:
        """Print the write that would have been sent."""
        print(f"[dry-run] {' '.join(build_set_duty_args(duty))}")

    def set_auto(self) -> None:
        """Print the write that would have been sent."""
        print(f"[dry-run] {' '.join(build_auto_args())}")

    def discover_dimm_slots(self, spec: DimmSpec) -> list[int]:
        """Delegate reads to the wrapped client."""
        return self._reader.discover_dimm_slots(spec)

    def read_sensors(
        self, tach: TachSpec, dimm: DimmSpec, dimm_slots: Sequence[int]
    ) -> IpmiSensors:
        """Delegate reads to the wrapped client."""
        return self._reader.read_sensors(tach, dimm, dimm_slots)

    def read_reference(self) -> IpmiSensors:
        """Delegate reads to the wrapped client."""
        return self._reader.read_reference()


def _run(args: list[str], timeout: float = TIMEOUT_S) -> str:
    """Run one ipmitool command, converting every failure into IpmiError."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as error:
        raise IpmiError(f"{IPMITOOL} not installed: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise IpmiError(f"{' '.join(args)} timed out after {timeout}s") from error
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        detail = lines[-1] if lines else "no output"
        raise IpmiError(f"{' '.join(args)} failed rc={result.returncode}: {detail}")
    return result.stdout
