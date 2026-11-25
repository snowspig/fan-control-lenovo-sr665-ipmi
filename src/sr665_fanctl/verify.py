"""Cross-check the fast raw sensor path against the slow SDR-based one.

The fast path reads sensors by (number, LUN) and converts them with constants
measured on this machine. If the BMC firmware ever changes the layout, a raw
read would silently return a *different* sensor's value (for example a DIMM
temperature of -40 C), which a thermal controller must never trust. So before
the controller starts, every fast reading is compared with what ipmitool
decodes from the SDR repository.
"""
from __future__ import annotations

from sr665_fanctl.config import Config
from sr665_fanctl.ipmi import IpmiClient, IpmiError

#: Fans may move a few LSBs between the two reads; anything larger is a mismatch.
TACH_TOLERANCE_RPM = 200
TACH_TOLERANCE_FRACTION = 0.05
#: Temperatures move slowly; a few degrees of drift between reads is normal.
TEMP_TOLERANCE_C = 3.0


def conversion_problems(client: IpmiClient, cfg: Config) -> list[str]:
    """Describe every disagreement between the raw path and the SDR path."""
    try:
        reference = client.read_reference()
        fast = client.read_sensors(cfg.tach, cfg.dimm, sorted(reference.dimm_temps))
    except IpmiError as error:
        return [f"sensor read failed: {error}"]
    if not reference.tach:
        return ["the SDR reference has no fan tach readings"]

    problems = []
    for fan, (front, rear) in sorted(reference.tach.items()):
        got = fast.tach.get(fan)
        if got is None:
            problems.append(f"fan {fan}: no raw tach reading")
            continue
        for label, expected, actual in (("front", front, got[0]), ("rear", rear, got[1])):
            if abs(expected - actual) > _tach_tolerance(expected):
                problems.append(
                    f"fan {fan} {label}: raw path {actual} rpm, reference {expected} rpm"
                )
    for slot, expected in sorted(reference.dimm_temps.items()):
        got = fast.dimm_temps.get(slot)
        if got is None:
            problems.append(f"dimm {slot}: no raw reading")
        elif abs(expected - got) > TEMP_TOLERANCE_C:
            problems.append(f"dimm {slot}: raw path {got} C, reference {expected} C")
    return problems


def _tach_tolerance(rpm: float) -> float:
    """How far a fan may drift between the reference read and the raw read."""
    return max(TACH_TOLERANCE_RPM, rpm * TACH_TOLERANCE_FRACTION)
