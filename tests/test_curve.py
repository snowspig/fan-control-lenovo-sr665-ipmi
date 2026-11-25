"""Curve maths and the decide() state machine (pure, no hardware).

Duties are OEM speed bytes (1..255): byte 30 is about 3040 rpm on this
machine, byte 255 is about 18400 rpm. The measured table is in
docs/probe-results.md.
"""
from dataclasses import replace

from sr665_fanctl.config import FULL_DUTY, Config
from sr665_fanctl.curve import State, Temps, decide, expected_min_rpm, interpolate, tach_faults

COOL = Temps(cpu1=39.0, cpu2=40.0, nvme_max=38.0, nvme_count=8, dimm_max=31.0, dimm_count=4)


def test_interpolate_clamps_below_the_first_point() -> None:
    assert interpolate(30.0, ((55, 30), (65, 60))) == 30


def test_interpolate_clamps_above_the_last_point() -> None:
    assert interpolate(90.0, ((55, 30), (65, 60))) == 60


def test_interpolate_is_linear_between_points() -> None:
    assert interpolate(60.0, ((55, 30), (65, 60))) == 45


def test_interpolate_returns_the_only_point_for_a_single_point_curve() -> None:
    assert interpolate(40.0, ((50, 60),)) == 60


def test_cool_machine_falls_one_slew_step_per_cycle() -> None:
    state = State(duty=100)
    decision, state = decide(state, COOL, Config())
    assert decision.duty == 84
    assert state.duty == 84


def test_cool_machine_reaches_min_duty_after_enough_cycles() -> None:
    state = State(duty=100)
    decision = None
    for _ in range(10):
        decision, state = decide(state, COOL, Config())
    assert decision.duty == 30


def test_hot_cpu_rises_without_slew_limiting() -> None:
    decision, _ = decide(State(duty=30), replace(COOL, cpu1=75.0), Config())
    assert decision.duty == 100


def test_hysteresis_holds_duty_within_two_degrees() -> None:
    cfg = Config()
    state = State(duty=30)
    _, state = decide(state, replace(COOL, cpu1=64.0), cfg)
    assert state.duty == 57
    anchor = state.last_curve["cpu"]
    decision, state = decide(state, replace(COOL, cpu1=65.9), cfg)
    assert decision.duty == 57
    assert state.last_curve["cpu"] == anchor


def test_hysteresis_recomputes_beyond_two_degrees() -> None:
    cfg = Config()
    state = State(duty=30)
    _, state = decide(state, replace(COOL, cpu1=64.0), cfg)
    decision, _ = decide(state, replace(COOL, cpu1=66.1), cfg)
    assert decision.duty == 64


def test_unreadable_class_never_makes_the_fans_slower() -> None:
    cfg = Config()
    state = State(duty=40)
    no_cpu = replace(COOL, cpu1=None, cpu2=None)
    decision, state = decide(state, no_cpu, cfg)
    assert decision.duty == 40
    decision, state = decide(state, no_cpu, cfg)
    assert decision.duty == 40


def test_three_unreadable_cycles_mean_blind_sensors() -> None:
    cfg = Config()
    state = State(duty=40)
    no_cpu = replace(COOL, cpu1=None, cpu2=None)
    for _ in range(3):
        decision, state = decide(state, no_cpu, cfg)
    assert decision.duty == FULL_DUTY
    assert decision.reason.startswith("blind")
    assert state.duty == FULL_DUTY


def test_failures_reset_when_readings_return() -> None:
    cfg = Config()
    state = State(duty=40)
    no_cpu = replace(COOL, cpu1=None, cpu2=None)
    _, state = decide(state, no_cpu, cfg)
    _, state = decide(state, no_cpu, cfg)
    assert state.failures["cpu"] == 2
    decision, state = decide(state, COOL, cfg)
    assert state.failures["cpu"] == 0
    assert decision.duty < 100


def test_emergency_overrides_curves_and_slew() -> None:
    decision, state = decide(State(duty=30), replace(COOL, nvme_max=79.0), Config())
    assert decision.duty == FULL_DUTY
    assert decision.reason == "emergency nvme 79C"
    assert state.duty == FULL_DUTY  # later cycles must slew down from full


def test_latched_tach_fault_keeps_full_speed() -> None:
    decision, state = decide(State(duty=30, forced_full=True), COOL, Config())
    assert decision.duty == FULL_DUTY
    assert "tach" in decision.reason
    assert state.duty == FULL_DUTY


def test_full_speed_is_left_by_slewing_not_by_jumping() -> None:
    hot = replace(COOL, nvme_max=79.0)
    _, state = decide(State(duty=30), hot, Config())
    decision, _ = decide(state, COOL, Config())  # sensor recovers, 39C
    assert decision.duty == FULL_DUTY - 16  # one slew step below full


def test_decision_is_never_below_the_hardware_floor() -> None:
    cfg = Config(
        hard_floor_duty=20, min_duty=1,
        cpu_curve=((55, 1),), dimm_curve=((70, 1),), nvme_curve=((55, 1),),
    )
    decision, _ = decide(State(duty=22), COOL, cfg)
    assert decision.duty == 20


def test_tach_faults_use_the_absolute_floor_without_a_measured_table() -> None:
    cfg = Config(fans=(1, 2), min_tach_rpm=1500)
    tach = {1: (8560, 8510), 2: (1200, 1400)}
    assert tach_faults(tach, 25, cfg) == ("fan 2: 1200 rpm < 1500 rpm at duty 25",)


def test_tach_faults_use_half_the_measured_rpm_when_available() -> None:
    cfg = Config(fans=(1,), expected_rpm=((25, 4000), (50, 8000), (100, 16000)))
    assert expected_min_rpm(25, cfg) == 2000
    assert tach_faults({1: (2100, 2050)}, 25, cfg) == ()
    assert tach_faults({1: (1900, 1950)}, 25, cfg) == ("fan 1: 1900 rpm < 2000 rpm at duty 25",)


def test_missing_tach_is_not_a_fault() -> None:
    """The BMC reports the tach as unavailable at the top speed sometimes."""
    cfg = Config(fans=(1, 2), min_tach_rpm=1500)
    assert tach_faults({1: (8560, 8510)}, 50, cfg) == ()


def test_expected_min_rpm_never_drops_below_the_absolute_floor() -> None:
    cfg = Config(expected_rpm=((25, 1000),), min_tach_rpm=1500)
    assert expected_min_rpm(25, cfg) == 1500
