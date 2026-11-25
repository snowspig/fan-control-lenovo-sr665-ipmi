"""Cross-check of the fast raw path against the SDR reference."""
from sr665_fanctl.config import Config
from sr665_fanctl.ipmi import IpmiError, IpmiSensors
from sr665_fanctl.verify import conversion_problems

CFG = Config()


class FakeClient:
    """Returns a fixed reference and a fixed fast reading."""

    def __init__(self, reference: IpmiSensors, fast: IpmiSensors,
                 fail: bool = False) -> None:
        self.reference = reference
        self.fast = fast
        self.fail = fail

    def read_reference(self) -> IpmiSensors:
        if self.fail:
            raise IpmiError("sensor list timed out")
        return self.reference

    def read_sensors(self, tach, dimm, slots) -> IpmiSensors:
        return self.fast


def test_agreeing_readings_have_no_problems() -> None:
    ref = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={1: 30.0})
    fast = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={1: 30.0})
    assert conversion_problems(FakeClient(ref, fast), CFG) == []


def test_small_drift_is_tolerated() -> None:
    ref = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={1: 30.0})
    fast = IpmiSensors(tach={1: (8400, 8700)}, dimm_temps={1: 32.0})
    assert conversion_problems(FakeClient(ref, fast), CFG) == []


def test_wrong_multiplier_is_reported_per_rotor() -> None:
    ref = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={1: 30.0})
    fast = IpmiSensors(tach={1: (8560, 9600)}, dimm_temps={1: 30.0})
    problems = conversion_problems(FakeClient(ref, fast), CFG)
    assert problems == ["fan 1 rear: raw path 9600 rpm, reference 8880 rpm"]


def test_wrong_lun_shows_a_wildly_off_temperature() -> None:
    ref = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={16: 31.0})
    fast = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={16: -40})
    problems = conversion_problems(FakeClient(ref, fast), CFG)
    assert problems == ["dimm 16: raw path -40 C, reference 31.0 C"]


def test_missing_fast_reading_is_reported() -> None:
    ref = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={1: 30.0})
    fast = IpmiSensors(tach={1: (8560, 8880)}, dimm_temps={})
    assert conversion_problems(FakeClient(ref, fast), CFG) == ["dimm 1: no raw reading"]


def test_reference_failure_is_reported_not_raised() -> None:
    problems = conversion_problems(
        FakeClient(IpmiSensors(tach={}, dimm_temps={}), IpmiSensors(tach={}, dimm_temps={}),
                   fail=True),
        CFG,
    )
    assert problems == ["sensor read failed: sensor list timed out"]
