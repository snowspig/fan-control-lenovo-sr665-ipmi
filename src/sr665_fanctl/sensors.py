"""Read CPU and NVMe temperatures from sysfs, independent of hwmon numbering.

Verified on this SR665 (kernel 6.8): k10temp chips expose ``Tctl`` as
``temp1_input``; NVMe chips are registered as hwmon ``name = "nvme"`` with
``temp1_input`` = Composite temperature. Both live under ``/sys/class/hwmon``;
``/sys/class/nvme/*/device/hwmon`` does not exist here.
"""
from __future__ import annotations

from pathlib import Path

from sr665_fanctl.curve import Temps
from sr665_fanctl.ipmi import IpmiSensors

HWMON_ROOT = Path("/sys/class/hwmon")
#: A dual-socket machine must expose exactly two k10temp instances.
EXPECTED_SOCKETS = 2


def read_temps(
    hwmon_root: Path = HWMON_ROOT, ipmi: IpmiSensors | None = None,
    nvme_temps: dict[str, float] | None = None,
) -> Temps:
    """Collect CPU, DIMM and NVMe readings into one snapshot for the controller.

    NVMe temperatures come from two places because the source depends on the
    driver in use: kernel ``nvme`` hwmon chips, and the HighPoint daemon
    (``nvme_temps``, refreshed in the background). Both are counted.
    """
    cpu1, cpu2 = read_socket_temps(hwmon_root)
    nvme = read_nvme_temps(hwmon_root) + list((nvme_temps or {}).values())
    dimm = list((ipmi.dimm_temps if ipmi is not None else {}).values())
    return Temps(
        cpu1=cpu1,
        cpu2=cpu2,
        nvme_max=max(nvme) if nvme else None,
        nvme_count=len(nvme),
        dimm_max=max(dimm) if dimm else None,
        dimm_count=len(dimm),
    )


def read_socket_temps(hwmon_root: Path = HWMON_ROOT) -> tuple[float | None, float | None]:
    """Tctl of both sockets, ordered by PCI address; (None, None) unless exactly two."""
    sockets: list[tuple[str, float]] = []
    for chip in sorted(hwmon_root.glob("**/hwmon*")):
        if _read(chip / "name") != "k10temp":
            continue
        tctl = _read_tctl(chip)
        if tctl is not None:
            sockets.append((_pci_address(chip) or chip.name, tctl))
    sockets.sort()
    if len(sockets) != EXPECTED_SOCKETS:
        return (None, None)
    return (sockets[0][1], sockets[1][1])


def read_nvme_temps(hwmon_root: Path = HWMON_ROOT) -> list[float]:
    """Composite temperature of every NVMe controller in degrees C."""
    values = []
    for chip in sorted(hwmon_root.glob("**/hwmon*")):
        if _read(chip / "name") != "nvme":
            continue
        value = _millidegrees(chip / "temp1_input")
        if value is not None:
            values.append(value)
    return values


def _read_tctl(chip: Path) -> float | None:
    """The Tctl reading of one k10temp chip; per-CCD temps are ignored."""
    fallback: float | None = None
    for temp_input in sorted(chip.glob("temp*_input")):
        value = _millidegrees(temp_input)
        if value is None:
            continue
        label_path = Path(str(temp_input).removesuffix("_input") + "_label")
        if _read(label_path) == "Tctl":
            return value
        if temp_input.name == "temp1_input":
            fallback = value
    return fallback


def find_pci_address(parts: tuple[str, ...]) -> str | None:
    """The last PCI-like component of a resolved sysfs path, e.g. ``0000:00:18.3``."""
    for part in reversed(parts):
        if part.count(":") == 2 and "." in part:
            return part
    return None


def _pci_address(chip: Path) -> str | None:
    """The PCI address a hwmon chip hangs off."""
    return find_pci_address(chip.resolve().parts)


def _read(path: Path) -> str | None:
    """Read a small sysfs attribute; None when it is missing or unreadable."""
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _millidegrees(path: Path) -> float | None:
    """Convert a hwmon millidegree reading to degrees C; None when unreadable."""
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw) / 1000
    except ValueError:
        return None
