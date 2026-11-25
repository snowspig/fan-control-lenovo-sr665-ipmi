"""Shared fixtures: the real ipmitool sample and a fake sysfs tree."""
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sensor_list_text() -> str:
    """Verbatim ``ipmitool sensor list`` output captured on the real server."""
    return (FIXTURES / "ipmitool_sensor_list.txt").read_text(encoding="ascii")


def make_hwmon(root, chip_dir: str, chip_name: str, temps: dict[str, int],
               labels: dict[str, str] | None = None):
    """Create one fake hwmon chip: ``root/chip_dir/name`` plus temp attributes."""
    chip = root / chip_dir
    chip.mkdir(parents=True, exist_ok=True)
    (chip / "name").write_text(chip_name, encoding="ascii")
    for attr, value in temps.items():
        (chip / attr).write_text(str(value), encoding="ascii")
    for attr, label in (labels or {}).items():
        (chip / attr).write_text(label, encoding="ascii")
    return chip


@pytest.fixture
def hwmon_root(tmp_path):
    """A fake /sys/class/hwmon with two k10temp chips and three nvme chips."""
    root = tmp_path / "hwmon"
    make_hwmon(root, "hwmon7", "k10temp", {"temp1_input": 45_000}, {"temp1_label": "Tctl"})
    make_hwmon(root, "hwmon2", "k10temp", {"temp1_input": 51_000}, {"temp1_label": "Tctl"})
    make_hwmon(root, "hwmon0", "nvme", {"temp1_input": 38_000}, {"temp1_label": "Composite"})
    make_hwmon(root, "hwmon5", "nvme", {"temp1_input": 62_500}, {"temp1_label": "Composite"})
    make_hwmon(root, "hwmon3", "nvme", {"temp1_input": 31_000}, {"temp1_label": "Composite"})
    return root


@pytest.fixture
def hpt_query_text() -> str:
    """``hptraidconf query devices 1/E1/1`` output (serial and write total anonymised)."""
    return (FIXTURES / "hptraidconf_query_devices.txt").read_text(encoding="ascii")
