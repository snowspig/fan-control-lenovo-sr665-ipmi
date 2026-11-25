"""sysfs temperature reading against a fake hwmon tree."""
from pathlib import Path

from conftest import make_hwmon

import sr665_fanctl.sensors as sensors_module
from sr665_fanctl.curve import Temps
from sr665_fanctl.ipmi import parse_sensor_list
from sr665_fanctl.sensors import (
    find_pci_address,
    read_nvme_temps,
    read_socket_temps,
    read_temps,
)


def test_two_sockets_are_reported_in_chip_order(hwmon_root: Path) -> None:
    assert read_socket_temps(hwmon_root) == (51.0, 45.0)


def test_tctl_label_wins_over_per_ccd_temps(tmp_path: Path) -> None:
    root = tmp_path / "hwmon"
    make_hwmon(root, "hwmon1", "k10temp", {"temp1_input": 33_000, "temp2_input": 41_000},
               {"temp1_label": "Tccd1", "temp2_label": "Tctl"})
    make_hwmon(root, "hwmon2", "k10temp", {"temp1_input": 51_000}, {"temp1_label": "Tctl"})
    assert read_socket_temps(root) == (41.0, 51.0)


def test_any_socket_count_other_than_two_is_a_failure(tmp_path: Path) -> None:
    root = tmp_path / "hwmon"
    make_hwmon(root, "hwmon1", "k10temp", {"temp1_input": 45_000}, {"temp1_label": "Tctl"})
    assert read_socket_temps(root) == (None, None)


def test_missing_k10temp_is_a_failure(tmp_path: Path) -> None:
    root = tmp_path / "hwmon"
    make_hwmon(root, "hwmon0", "nvme", {"temp1_input": 38_000}, {"temp1_label": "Composite"})
    assert read_socket_temps(root) == (None, None)


def test_pci_address_is_found_in_a_resolved_path() -> None:
    parts = ("sys", "devices", "pci0000:00", "0000:00:19.3", "hwmon", "hwmon9")
    assert find_pci_address(parts) == "0000:00:19.3"


def test_pci_address_is_none_without_a_pci_component() -> None:
    assert find_pci_address(("hwmon", "hwmon2")) is None


def test_socket_order_follows_the_pci_address(tmp_path: Path, monkeypatch) -> None:
    # Windows cannot create ":"-named directories, so inject the addresses.
    root = tmp_path / "hwmon"
    make_hwmon(root, "hwmon9", "k10temp", {"temp1_input": 46_000}, {"temp1_label": "Tctl"})
    make_hwmon(root, "hwmon1", "k10temp", {"temp1_input": 45_000}, {"temp1_label": "Tctl"})
    addresses = {"hwmon9": "0000:00:19.3", "hwmon1": "0000:00:18.3"}
    monkeypatch.setattr(sensors_module, "_pci_address", lambda chip: addresses[chip.name])
    assert read_socket_temps(root) == (45.0, 46.0)


def test_nvme_temps_read_every_controller(hwmon_root: Path) -> None:
    assert read_nvme_temps(hwmon_root) == [38.0, 31.0, 62.5]


def test_read_temps_merges_sysfs_and_ipmi(hwmon_root: Path, sensor_list_text: str) -> None:
    temps = read_temps(hwmon_root, parse_sensor_list(sensor_list_text))
    assert temps == Temps(cpu1=51.0, cpu2=45.0, nvme_max=62.5, nvme_count=3,
                          dimm_max=33.0, dimm_count=4)


def test_read_temps_without_ipmi_marks_dimm_unreadable(hwmon_root: Path) -> None:
    temps = read_temps(hwmon_root, None)
    assert temps.dimm_max is None
    assert temps.dimm_count == 0
