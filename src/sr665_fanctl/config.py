"""Configuration: defaults, TOML overrides and safety validation.

``hard_floor_duty = 0`` is a sentinel meaning "the Phase 0 probe has not
measured the hardware floor yet"; ``fanctl run`` refuses to start on it.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

Curve = tuple[tuple[float, int], ...]

DEFAULT_CONFIG_PATH = Path("/etc/fanctl/config.toml")
#: Fan ids of the chassis in IPMI numbering.
CHASSIS_FANS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
#: The top speed byte: what every safety path sends (about 18400 rpm here).
FULL_DUTY: int = 255
#: The three temperature classes the controller tracks; each has its own curve.
CLASSES: tuple[str, ...] = ("cpu", "dimm", "nvme")


class ConfigError(ValueError):
    """The configuration is invalid and the controller must not start."""


@dataclass(frozen=True)
class TachSpec:
    """Where the fan tach sensors live and how their raw bytes convert to RPM.

    Values measured on this SR665 (XCC 3.01); the front and rear rotors use
    different multipliers, so they are configured separately.
    """

    lun: int = 0
    front_numbers: tuple[int, ...] = (0x41, 0x42, 0x43, 0x44, 0x45, 0x46)
    rear_numbers: tuple[int, ...] = (0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E)
    front_m: float = 80.0
    rear_m: float = 74.0
    b: float = 0.0


@dataclass(frozen=True)
class DimmSpec:
    """DIMM temperature sensors: slot ``n`` is sensor ``base_number + n``.

    Slots are discovered at startup (an empty slot refuses the read), so only
    the address scheme is configured.
    """

    lun: int = 1
    base_number: int = 0x50
    slot_count: int = 32
    m: float = 1.0
    b: float = 0.0


@dataclass(frozen=True)
class NvmeSpec:
    """Where the NVMe drive temperatures come from.

    ``k10temp``/``nvme`` sysfs chips disappear when the HighPoint driver
    (``hptnvme``) claims the drives, so the SMART temperatures are read from
    ``hptsvr`` through ``hptraidconf`` (local TCP, no root needed). Each drive
    query takes ~3 s and hptsvr serialises them, hence the background refresh.
    """

    devices: tuple[str, ...] = (
        "1/E1/1", "1/E1/2", "1/E1/3", "1/E1/4",
        "1/E1/5", "1/E1/6", "1/E1/7", "1/E1/8",
    )
    binary: str = "hptraidconf"
    user: str = "RAID"
    password: str = "hpt"
    timeout_s: float = 15.0
    pause_s: float = 2.0
    stale_s: float = 90.0


@dataclass(frozen=True)
class Config:
    """Every tunable of the controller; the spec explains each value."""

    interval_s: float = 5.0
    fans: tuple[int, ...] = CHASSIS_FANS  # for tach verification only
    #: Duty is the OEM speed byte sent to the BMC: 1..255 (0 means "automatic").
    #: The byte maps to about 80 rpm per unit on this machine; the measured
    #: table is ``expected_rpm`` and the calibration is in docs/probe-results.md.
    min_duty: int = 30
    hard_floor_duty: int = 0  # 0 = not measured yet
    safe_duty: int = 100
    hysteresis_c: float = 2.0
    max_fall_per_cycle: int = 16
    sensor_fail_cycles: int = 3
    tach_fail_cycles: int = 2
    min_tach_rpm: int = 1500
    min_tach_fraction: float = 0.5
    emergency_c: dict[str, float] = field(
        default_factory=lambda: {"cpu": 85.0, "dimm": 85.0, "nvme": 78.0}
    )
    cpu_curve: Curve = ((55, 30), (65, 60), (75, 100), (85, 255))
    dimm_curve: Curve = ((70, 30), (78, 60), (85, 255))
    nvme_curve: Curve = ((55, 30), (65, 60), (72, 100), (78, 255))
    expected_rpm: Curve = ()
    tach: TachSpec = field(default_factory=TachSpec)
    dimm: DimmSpec = field(default_factory=DimmSpec)
    nvme: NvmeSpec = field(default_factory=NvmeSpec)


def load_config(path: Path | None = None) -> Config:
    """Defaults overridden by ``path`` (or the default file when it is None).

    A missing file is not an error: the built-in defaults apply.
    """
    target = DEFAULT_CONFIG_PATH if path is None else path
    if not target.is_file():
        return validate(Config())
    with target.open("rb") as handle:
        return validate(Config(**_coerce(tomllib.load(handle))))


def validate(cfg: Config, *, require_measured_floor: bool = False) -> Config:
    """Reject configurations that could drive the fans unsafely; return ``cfg``.

    ``require_measured_floor`` is set by ``fanctl run``: the hardware floor has
    to come from the Phase 0 probe, never from a guess.
    """
    if require_measured_floor and cfg.hard_floor_duty <= 0:
        raise ConfigError(
            "hard_floor_duty is not measured yet; run the Phase 0 probe "
            "(docs/probe-results.md) before starting the service"
        )
    if not 0 <= cfg.hard_floor_duty <= cfg.min_duty <= cfg.safe_duty <= 255:
        raise ConfigError(
            "expected 0 <= hard_floor_duty <= min_duty <= safe_duty <= 255, got "
            f"{cfg.hard_floor_duty}, {cfg.min_duty}, {cfg.safe_duty}"
        )
    if not cfg.fans or not all(0 <= fan <= 0xFF for fan in cfg.fans):
        raise ConfigError("fans must list ids in 0..255")
    if cfg.interval_s <= 0:
        raise ConfigError("interval_s must be > 0")
    if cfg.max_fall_per_cycle < 1 or cfg.sensor_fail_cycles < 1 or cfg.tach_fail_cycles < 1:
        raise ConfigError("max_fall_per_cycle and the fail-cycle counts must be >= 1")
    if set(cfg.emergency_c) != set(CLASSES):
        raise ConfigError(f"emergency_c must define exactly {CLASSES}")
    _validate_tach(cfg.tach)
    _validate_dimm(cfg.dimm)
    _validate_nvme(cfg.nvme)
    for name in ("cpu_curve", "dimm_curve", "nvme_curve"):
        _check_curve(name, getattr(cfg, name), is_temperature=True)
    if cfg.expected_rpm:
        _check_curve("expected_rpm", cfg.expected_rpm, is_temperature=False)
    return cfg


def _coerce(data: dict) -> dict:
    """Turn TOML arrays into the exact types Config declares; reject typos."""
    known = {field_.name for field_ in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown config keys: {unknown}")
    coerced = dict(data)
    if "fans" in coerced:
        coerced["fans"] = tuple(int(fan) for fan in coerced["fans"])
    for name in ("cpu_curve", "dimm_curve", "nvme_curve", "expected_rpm"):
        if name in coerced:
            coerced[name] = tuple((float(x), int(y)) for x, y in coerced[name])
    for name, spec_type in (("tach", TachSpec), ("dimm", DimmSpec), ("nvme", NvmeSpec)):
        if name in coerced:
            coerced[name] = _coerce_spec(spec_type, coerced[name], name)
    return coerced


def _coerce_spec(spec_type: type, data: dict, name: str):
    """Build a sensor spec dataclass from a TOML table, rejecting typos."""
    known = {field_.name for field_ in fields(spec_type)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown {name} keys: {unknown}")
    values = dict(data)
    for key, value in values.items():
        if isinstance(value, list):
            values[key] = tuple(value)
    return spec_type(**values)


def _validate_tach(tach: TachSpec) -> None:
    """The front/rear number lists must pair up one-to-one."""
    if len(tach.front_numbers) != len(tach.rear_numbers):
        raise ConfigError(
            "tach front_numbers and rear_numbers must have the same length: "
            f"{len(tach.front_numbers)} vs {len(tach.rear_numbers)}"
        )
    if not tach.front_numbers:
        raise ConfigError("tach front_numbers must not be empty")
    for label, numbers in (("front", tach.front_numbers), ("rear", tach.rear_numbers)):
        if any(not 0 <= number <= 0xFF for number in numbers):
            raise ConfigError(f"tach {label}_numbers must be in 0x00..0xff")
    if not 0 <= tach.lun <= 3:
        raise ConfigError("tach lun must be 0..3")
    if tach.front_m <= 0 or tach.rear_m <= 0:
        raise ConfigError("tach multipliers must be > 0")


def _validate_dimm(dimm: DimmSpec) -> None:
    """The DIMM address scheme must stay inside one sensor address space."""
    if not 0 <= dimm.lun <= 3:
        raise ConfigError("dimm lun must be 0..3")
    if not 0 <= dimm.base_number <= 0xFF:
        raise ConfigError("dimm base_number must be in 0x00..0xff")
    if not 1 <= dimm.slot_count <= 0xFF - dimm.base_number:
        raise ConfigError(f"dimm slot_count must fit under 0xff from base {dimm.base_number:#x}")
    if dimm.m <= 0:
        raise ConfigError("dimm multiplier must be > 0")


def _validate_nvme(nvme: NvmeSpec) -> None:
    """Keep the background refresh bounded by its staleness window."""
    if not 0 < nvme.timeout_s < nvme.stale_s:
        raise ConfigError("nvme timeout_s must be > 0 and < stale_s")
    if nvme.pause_s < 0:
        raise ConfigError("nvme pause_s must be >= 0")
    if not nvme.binary:
        raise ConfigError("nvme binary must not be empty")


def _check_curve(name: str, curve: Curve, *, is_temperature: bool) -> None:
    """Curves need a strictly increasing x axis; duties are 1..255."""
    xs = [x for x, _ in curve]
    if not xs:
        raise ConfigError(f"{name} must have at least one point")
    if any(b <= a for a, b in zip(xs, xs[1:])):
        raise ConfigError(f"{name} x values must be strictly increasing: {xs}")
    if is_temperature and any(not 1 <= y <= 255 for _, y in curve):
        raise ConfigError(f"{name} duties must be in 1..255 (0 means automatic)")
    if not is_temperature and any(y < 0 for _, y in curve):
        raise ConfigError(f"{name} rpm values must be >= 0")
