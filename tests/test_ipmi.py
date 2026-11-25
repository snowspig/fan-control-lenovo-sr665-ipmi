"""ipmitool argv building, raw reading, output parsing and error mapping."""
import subprocess

import pytest

import sr665_fanctl.ipmi as ipmi_module
from sr665_fanctl.config import DimmSpec, TachSpec
from sr665_fanctl.ipmi import (
    DryRunClient,
    IpmiClient,
    IpmiError,
    IpmiSensors,
    build_auto_args,
    build_read_args,
    build_set_duty_args,
    parse_raw_reading,
    parse_sensor_list,
)


def test_build_set_duty_args_matches_the_oem_layout() -> None:
    assert build_set_duty_args(40) == [
        "ipmitool", "raw", "0x3a", "0x07", "0xff", "0x28", "0x01",
    ]


def test_build_auto_args_hands_control_back_to_the_bmc() -> None:
    assert build_auto_args() == [
        "ipmitool", "raw", "0x3a", "0x07", "0xff", "0x00", "0x00",
    ]


def test_build_set_duty_args_rejects_the_reserved_and_out_of_range_bytes() -> None:
    with pytest.raises(ValueError, match="reserved for automatic"):
        build_set_duty_args(0)
    with pytest.raises(ValueError, match="outside"):
        build_set_duty_args(256)


def test_build_read_args_addresses_the_right_lun() -> None:
    assert build_read_args(0x51, 1) == [
        "ipmitool", "-l", "1", "raw", "0x04", "0x2d", "0x51",
    ]


def test_parse_raw_reading_takes_the_first_byte() -> None:
    assert parse_raw_reading(" 67 c0 c0\n") == 0x67


def test_parse_raw_reading_rejects_unavailable_readings() -> None:
    assert parse_raw_reading(" ff c0 c0\n") is None  # no reading
    assert parse_raw_reading(" 67 e0 c0\n") is None  # reading unavailable
    assert parse_raw_reading(" 67 80 c0\n") is None  # sensor scanning disabled
    assert parse_raw_reading("") is None
    assert parse_raw_reading("not hex\n") is None


def test_parse_sensor_list_reads_front_and_rear_tach(sensor_list_text: str) -> None:
    sensors = parse_sensor_list(sensor_list_text)
    assert set(sensors.tach) == {1, 2, 3, 4, 5, 6}
    assert sensors.tach[1] == (8560, 8510)
    assert sensors.tach[3] == (8240, 8880)


def test_parse_sensor_list_skips_empty_dimm_slots(sensor_list_text: str) -> None:
    sensors = parse_sensor_list(sensor_list_text)
    assert sensors.dimm_temps == {1: 30.0, 16: 31.0, 17: 31.0, 32: 33.0}


def test_parse_sensor_list_ignores_discrete_fan_sensors(sensor_list_text: str) -> None:
    sensors = parse_sensor_list(sensor_list_text)
    assert 0 not in sensors.tach  # "Fan Mismatch" must not become fan 0


def test_read_sensors_scales_each_rotor_by_its_own_factor() -> None:
    tach = TachSpec(front_numbers=(0x41,), rear_numbers=(0x49,))
    readings = {"0x41": " 67 c0 c0", "0x49": " 78 c0 c0"}

    def runner(args, timeout) -> str:
        return readings[args[-1]]

    client = IpmiClient(runner=runner)
    sensors = client.read_sensors(tach, DimmSpec(), [])
    assert sensors.tach == {1: (8240, 8880)}  # 0x67=103 -> 103*80; 0x78=120 -> 120*74


def test_read_sensors_reads_dimm_slots_from_their_base_number() -> None:
    def runner(args, timeout) -> str:
        return " 1e c0 c0" if args[-1] == "0x51" else " 00 c0 c0"

    client = IpmiClient(runner=runner)
    sensors = client.read_sensors(TachSpec(), DimmSpec(), [1])
    assert sensors.dimm_temps == {1: 30}  # 0x1e = 30


def test_read_sensors_raises_when_every_read_fails() -> None:
    def runner(args, timeout) -> str:
        raise IpmiError("rsp=0xcb")

    client = IpmiClient(runner=runner)
    with pytest.raises(IpmiError, match="all 2 sensor reads failed"):
        client.read_sensors(TachSpec(front_numbers=(0x41,), rear_numbers=(0x49,)),
                             DimmSpec(), [])


def test_read_sensors_keeps_the_sensors_that_answered() -> None:
    def runner(args, timeout) -> str:
        if args[-1] == "0x49":
            raise IpmiError("rsp=0xcb")
        return " 67 c0 c0"

    client = IpmiClient(runner=runner)
    sensors = client.read_sensors(TachSpec(front_numbers=(0x41,), rear_numbers=(0x49,)),
                                  DimmSpec(), [])
    assert sensors.tach == {}  # a fan needs both rotors


def test_discover_dimm_slots_keeps_only_slots_that_answer() -> None:
    def runner(args, timeout) -> str:
        if args[-1] in ("0x51", "0x52"):  # slots 1 and 2 of base 0x50
            return " 1e c0 c0"
        raise IpmiError("rsp=0xcb")

    client = IpmiClient(runner=runner)
    assert client.discover_dimm_slots(DimmSpec(slot_count=3)) == [1, 2]


def test_read_reference_uses_the_slow_sdr_path(sensor_list_text: str) -> None:
    seen: list[tuple[list[str], float]] = []

    def runner(args, timeout) -> str:
        seen.append((args, timeout))
        return sensor_list_text

    sensors = IpmiClient(runner=runner).read_reference()
    assert sensors.tach[1] == (8560, 8510)
    assert seen[0][0] == ["ipmitool", "sensor", "list"]
    assert seen[0][1] >= 30  # the slow path needs a generous timeout


def test_set_duty_sends_the_built_command() -> None:
    seen: list[list[str]] = []
    client = IpmiClient(runner=lambda args, timeout: seen.append(args) and "")
    client.set_duty(35)
    assert seen == [["ipmitool", "raw", "0x3a", "0x07", "0xff", "0x23", "0x01"]]


def test_set_auto_sends_the_auto_command() -> None:
    seen: list[list[str]] = []
    client = IpmiClient(runner=lambda args, timeout: seen.append(args) and "")
    client.set_auto()
    assert seen == [["ipmitool", "raw", "0x3a", "0x07", "0xff", "0x00", "0x00"]]


def test_nonzero_exit_becomes_ipmi_error(monkeypatch) -> None:
    result = subprocess.CompletedProcess(
        args=["ipmitool", "raw"], returncode=1, stdout="",
        stderr="Unable to send RAW command: rsp=0xc1\n",
    )
    monkeypatch.setattr(ipmi_module.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(IpmiError, match="rsp=0xc1"):
        ipmi_module._run(["ipmitool", "raw"])


def test_timeout_becomes_ipmi_error(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ipmitool", timeout=10)

    monkeypatch.setattr(ipmi_module.subprocess, "run", boom)
    with pytest.raises(IpmiError, match="timed out"):
        ipmi_module._run(["ipmitool", "sensor", "list"])


def test_dry_run_prints_writes_and_delegates_reads(capsys) -> None:
    class Reader:
        def discover_dimm_slots(self, spec):
            return [4]

        def read_sensors(self, tach, dimm, dimm_slots):
            return IpmiSensors(tach={}, dimm_temps={4: 33.0})

        def read_reference(self):
            return IpmiSensors(tach={}, dimm_temps={4: 33.0})

    client = DryRunClient(Reader())
    client.set_duty(40)
    assert capsys.readouterr().out.strip() == "[dry-run] ipmitool raw 0x3a 0x07 0xff 0x28 0x01"
    client.set_auto()
    assert capsys.readouterr().out.strip() == "[dry-run] ipmitool raw 0x3a 0x07 0xff 0x00 0x00"
    assert client.discover_dimm_slots(DimmSpec()) == [4]
    assert client.read_sensors(TachSpec(), DimmSpec(), [4]).dimm_temps == {4: 33.0}
    assert client.read_reference().dimm_temps == {4: 33.0}
