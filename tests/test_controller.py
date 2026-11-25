"""The control loop against a scripted fake BMC."""
import logging

import pytest

from sr665_fanctl.config import Config
from sr665_fanctl.controller import Controller
from sr665_fanctl.ipmi import IpmiError, IpmiSensors


class FakeClient:
    """Scripted BMC: hands out one IpmiSensors snapshot per read, records writes."""

    def __init__(self, sensors: list[IpmiSensors], slots: tuple[int, ...] = (1,)) -> None:
        self.sensors = list(sensors)
        self.default = IpmiSensors(tach={1: (8500, 8500)}, dimm_temps={1: 30.0})
        self.slots = list(slots)
        self.writes: list[tuple[int, int]] = []
        self.fail_writes = 0
        self.discoveries = 0

    def set_duty(self, duty: int) -> None:
        if self.fail_writes:
            self.fail_writes -= 1
            raise IpmiError("bmc refused the write")
        self.writes.append(duty)

    def discover_dimm_slots(self, spec) -> list[int]:
        self.discoveries += 1
        return list(self.slots)

    def read_sensors(self, tach, dimm, dimm_slots) -> IpmiSensors:
        return self.sensors.pop(0) if self.sensors else self.default


class BrokenReadClient(FakeClient):
    """Every read fails, as if the BMC vanished."""

    def read_sensors(self, tach, dimm, dimm_slots) -> IpmiSensors:
        raise IpmiError("bmc unreachable")


def make_controller(hwmon_root, client, **overrides) -> Controller:
    cfg = Config(hard_floor_duty=20, fans=(1,), **overrides)
    return Controller(client, cfg, hwmon_root=hwmon_root)


def test_first_cycle_writes_one_slew_step_to_all_fans(hwmon_root) -> None:
    client = FakeClient([])
    controller = make_controller(hwmon_root, client)
    decision = controller.run_cycle()
    assert decision.duty == 84
    assert client.writes == [84]
    assert controller.state.sent is True
    assert client.discoveries == 1


def test_dimm_discovery_is_retried_when_it_answers_nothing(hwmon_root) -> None:
    client = FakeClient([], slots=())
    controller = make_controller(hwmon_root, client)
    controller.run_cycle()
    controller.run_cycle()
    assert client.discoveries == 2  # an empty discovery is not taken as final


def test_slow_tach_latches_full_speed_after_two_cycles(hwmon_root) -> None:
    slow = IpmiSensors(tach={1: (1200, 1400)}, dimm_temps={1: 30.0})
    client = FakeClient([slow, slow, slow])
    controller = make_controller(hwmon_root, client)
    controller.run_cycle()
    controller.run_cycle()
    assert controller.state.tach_faults == 1
    controller.run_cycle()
    assert controller.state.forced_full is True
    assert client.writes[-1] == 255


def test_healthy_tach_releases_the_full_speed_latch(hwmon_root) -> None:
    slow = IpmiSensors(tach={1: (1200, 1400)}, dimm_temps={1: 30.0})
    fast = IpmiSensors(tach={1: (9000, 9000)}, dimm_temps={1: 30.0})
    client = FakeClient([slow, slow, slow, fast])  # latch on cycle 3, release on 4
    controller = make_controller(hwmon_root, client)
    controller.run_cycle()
    controller.run_cycle()
    controller.run_cycle()
    controller.run_cycle()
    assert controller.state.forced_full is False
    assert client.writes[-1] == 239  # ramp down from 255 by one slew step


def test_write_failure_retries_once_then_succeeds(hwmon_root) -> None:
    client = FakeClient([])
    client.fail_writes = 1
    controller = make_controller(hwmon_root, client)
    assert controller.run_cycle().duty == 84
    assert client.writes == [84]


def test_two_write_failures_are_fatal(hwmon_root) -> None:
    client = FakeClient([])
    client.fail_writes = 2
    controller = make_controller(hwmon_root, client)
    with pytest.raises(IpmiError):
        controller.run_cycle()


def test_ipmi_read_failure_falls_back_to_sysfs_and_holds(hwmon_root) -> None:
    client = BrokenReadClient([])
    controller = make_controller(hwmon_root, client)
    controller.run_cycle()
    assert controller.state.failures["dimm"] == 1
    assert client.writes == [100]  # no fall without DIMM data


def test_disappearing_dimm_sensor_is_warned(hwmon_root, caplog) -> None:
    two = IpmiSensors(tach={1: (8500, 8500)}, dimm_temps={1: 30.0, 16: 31.0})
    one = IpmiSensors(tach={1: (8500, 8500)}, dimm_temps={1: 30.0})
    client = FakeClient([two, one])
    controller = make_controller(hwmon_root, client)
    controller.run_cycle()
    assert controller.state.baseline == {"nvme": 3, "dimm": 2}
    with caplog.at_level(logging.WARNING, logger="sr665_fanctl.controller"):
        controller.run_cycle()
    assert "dimm sensor count dropped: 2 -> 1" in caplog.text


def test_temperature_sequence_drives_the_duty(hwmon_root) -> None:
    """A scripted temperature sequence: heat up, then cool down."""
    client = FakeClient([])
    controller = make_controller(hwmon_root, client)
    (hwmon_root / "hwmon3" / "temp1_input").write_text("38000", encoding="ascii")
    (hwmon_root / "hwmon0" / "temp1_input").write_text("38000", encoding="ascii")
    (hwmon_root / "hwmon5" / "temp1_input").write_text("62000", encoding="ascii")
    duties = []
    for cpu in (40_000, 70_000, 76_000, 40_000):
        (hwmon_root / "hwmon2" / "temp1_input").write_text(str(cpu), encoding="ascii")
        duties.append(controller.run_cycle().duty)
    # cycle 1: cool, slew 100 -> 84 (nvme 62C wants byte 51);
    # cycle 2: cpu 70C -> 80 (within the slew limit); cycle 3: cpu 76C -> 116;
    # cycle 4: cool again, slew-limited fall 116 -> 100.
    assert duties == [84, 80, 116, 100]


class StopLoop(BaseException):
    """Raised by the fake sleep to end run_forever in a test."""


def limited_sleep(calls: list[float], limit: int):
    def _sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) >= limit:
            raise StopLoop
    return _sleep


def test_run_forever_paces_to_the_interval(hwmon_root) -> None:
    calls: list[float] = []
    cfg = Config(hard_floor_duty=20, fans=(1,), interval_s=5.0)
    controller = Controller(FakeClient([]), cfg, hwmon_root=hwmon_root,
                            sleep=limited_sleep(calls, 3))
    with pytest.raises(StopLoop):
        controller.run_forever()
    assert len(calls) == 3
    assert all(4.5 < seconds <= 5.0 for seconds in calls)
