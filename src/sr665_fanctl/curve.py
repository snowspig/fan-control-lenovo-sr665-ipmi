"""Pure decision logic: temperatures in, a fan duty out.

Nothing here touches I/O, so the whole safety behaviour is unit-testable
without hardware.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from sr665_fanctl.config import CLASSES, FULL_DUTY, Config, Curve


@dataclass(frozen=True)
class Temps:
    """One snapshot of every temperature class.

    ``None`` means "unreadable this cycle" and is handled as a sensor failure;
    the counts let the controller notice hardware that disappeared.
    """

    cpu1: float | None
    cpu2: float | None
    nvme_max: float | None
    nvme_count: int
    dimm_max: float | None
    dimm_count: int


@dataclass(frozen=True)
class Decision:
    """The duty to send this cycle plus a short human-readable reason."""

    duty: int
    reason: str


@dataclass
class State:
    """Controller memory carried from cycle to cycle."""

    duty: int
    sent: bool = False
    forced_full: bool = False
    tach_faults: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    last_curve: dict[str, tuple[float, int]] = field(default_factory=dict)
    baseline: dict[str, int] | None = None


def interpolate(value: float, curve: Curve) -> int:
    """Piecewise-linear lookup of ``value`` in ``curve``, clamped at both ends."""
    points = sorted(curve)
    if value <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            return round(y0 + (value - x0) * (y1 - y0) / (x1 - x0))
    return points[-1][1]


def class_value(temps: Temps, name: str) -> float | None:
    """The temperature one class is judged by, or None when it is unreadable."""
    if name == "cpu":
        if temps.cpu1 is None or temps.cpu2 is None:
            return None
        return max(temps.cpu1, temps.cpu2)
    if name == "dimm":
        return temps.dimm_max
    return temps.nvme_max


def decide(state: State, temps: Temps, cfg: Config) -> tuple[Decision, State]:
    """Work out this cycle's duty as a pure function of (state, temps, cfg).

    Precedence: latched tach fault > blind sensors > emergency temperature >
    curves with hysteresis and slew limiting.
    """
    failures = {
        name: (state.failures.get(name, 0) + 1) if class_value(temps, name) is None else 0
        for name in CLASSES
    }
    state = replace(state, failures=failures)
    if state.forced_full:
        return _full(state, "tach fault latched at full speed")
    if max(failures.values()) >= cfg.sensor_fail_cycles:
        return _full(state, "blind sensors: no trustworthy temperature")
    emergency = _emergency(temps, cfg)
    if emergency is not None:
        return _full(state, emergency)
    targets, last_curve = _curve_targets(state, temps, cfg)
    target = max([cfg.min_duty, *targets.values()])
    incomplete = len(targets) < len(CLASSES)
    if incomplete:
        target = max(target, state.duty)  # never slow down on incomplete data
    duty = max(_slew(state.duty, target, cfg.max_fall_per_cycle), cfg.hard_floor_duty)
    return Decision(duty, _reason(temps, incomplete)), replace(
        state, duty=duty, last_curve=last_curve
    )


def _full(state: State, reason: str) -> tuple[Decision, State]:
    """Full speed, recorded in the state so later cycles slew down from it."""
    return Decision(FULL_DUTY, reason), replace(state, duty=FULL_DUTY)


def _emergency(temps: Temps, cfg: Config) -> str | None:
    """The first class past its emergency threshold, as a reason string."""
    for name in CLASSES:
        value = class_value(temps, name)
        if value is not None and value >= cfg.emergency_c[name]:
            return f"emergency {name} {value:.0f}C"
    return None


def _curve_targets(
    state: State, temps: Temps, cfg: Config
) -> tuple[dict[str, int], dict[str, tuple[float, int]]]:
    """Curve duty per readable class, with per-class temperature hysteresis."""
    curves = {"cpu": cfg.cpu_curve, "dimm": cfg.dimm_curve, "nvme": cfg.nvme_curve}
    targets: dict[str, int] = {}
    last_curve = dict(state.last_curve)
    for name in CLASSES:
        value = class_value(temps, name)
        if value is None:
            continue
        anchor = last_curve.get(name)
        if anchor is not None and abs(value - anchor[0]) < cfg.hysteresis_c:
            targets[name] = anchor[1]
            continue
        targets[name] = interpolate(value, curves[name])
        last_curve[name] = (value, targets[name])
    return targets, last_curve


def _slew(current: int, target: int, max_fall: int) -> int:
    """Allow instant speed-up but limit how fast the fans may slow down."""
    if target >= current:
        return target
    return max(target, current - max_fall)


def _reason(temps: Temps, incomplete: bool) -> str:
    """Readable temperatures as ``cpu:39C dimm:31C nvme:38C`` plus a hold marker."""
    parts = [
        f"{name}:{value:.0f}C"
        for name in CLASSES
        if (value := class_value(temps, name)) is not None
    ]
    return " ".join(parts) + (" +hold (sensor unreadable)" if incomplete else "")


def expected_min_rpm(duty: int, cfg: Config) -> int:
    """Lowest RPM acceptable at ``duty``: half the measured value, never below min_tach_rpm."""
    if not cfg.expected_rpm:
        return cfg.min_tach_rpm
    measured = interpolate(duty, cfg.expected_rpm)
    return max(int(measured * cfg.min_tach_fraction), cfg.min_tach_rpm)


def tach_faults(tach: dict[int, tuple[int, int]], duty: int, cfg: Config) -> tuple[str, ...]:
    """Describe every fan spinning too slowly for ``duty``; empty means healthy.

    A *missing* reading is not treated as a fault: at the highest speed the
    BMC occasionally reports the tach as unavailable (0xff) even though the
    fan is spinning. A fan that really stopped reports a number (0), and that
    is caught by the rpm comparison.
    """
    floor = expected_min_rpm(duty, cfg)
    faults = []
    for fan in cfg.fans:
        speeds = tach.get(fan)
        if speeds is None:
            continue
        if min(speeds) < floor:
            faults.append(f"fan {fan}: {min(speeds)} rpm < {floor} rpm at duty {duty}")
    return tuple(faults)
