"""Configuration loading, type coercion and safety validation."""
import pytest

from sr665_fanctl.config import (
    Config,
    ConfigError,
    DimmSpec,
    TachSpec,
    load_config,
    validate,
)


def test_defaults_load_without_a_file(tmp_path) -> None:
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.min_duty == 30
    assert cfg.hard_floor_duty == 0


def test_toml_overrides_defaults(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "min_duty = 30\nfans = [2, 4]\ncpu_curve = [[50, 30], [80, 100]]\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.min_duty == 30
    assert cfg.fans == (2, 4)
    assert cfg.cpu_curve == ((50.0, 30), (80.0, 100))


def test_unknown_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("min_dutye = 30\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(path)


def test_floor_above_min_duty_is_rejected() -> None:
    with pytest.raises(ConfigError, match="hard_floor_duty"):
        validate(Config(hard_floor_duty=30, min_duty=25))


def test_run_requires_a_measured_floor() -> None:
    with pytest.raises(ConfigError, match="Phase 0"):
        validate(Config(), require_measured_floor=True)


def test_measured_floor_is_accepted() -> None:
    assert validate(Config(hard_floor_duty=20), require_measured_floor=True).hard_floor_duty == 20


def test_decreasing_curve_is_rejected() -> None:
    with pytest.raises(ConfigError, match="strictly increasing"):
        validate(Config(cpu_curve=((65, 40), (55, 20))))


def test_zero_duty_point_is_rejected() -> None:
    with pytest.raises(ConfigError, match="1..255"):
        validate(Config(nvme_curve=((55, 0), (78, 100))))


def test_sensor_specs_parse_from_toml(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[tach]\nfront_numbers = [0x41, 0x42]\nrear_numbers = [0x49, 0x4a]\n"
        "front_m = 80\nrear_m = 74\n\n[dimm]\nbase_number = 0x50\nslot_count = 32\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.tach.front_numbers == (0x41, 0x42)
    assert cfg.tach.rear_m == 74
    assert cfg.dimm.base_number == 0x50
    assert cfg.dimm.lun == 1


def test_unknown_sensor_spec_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[tach]\nfront_m = 80\nfron_m = 80\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown tach keys"):
        load_config(path)


def test_mismatched_tach_lists_are_rejected() -> None:
    with pytest.raises(ConfigError, match="same length"):
        validate(Config(tach=TachSpec(front_numbers=(0x41,), rear_numbers=())))


def test_non_positive_multiplier_is_rejected() -> None:
    with pytest.raises(ConfigError, match="multipliers must be > 0"):
        validate(Config(tach=TachSpec(front_m=0.0)))


def test_dimm_slot_count_must_fit_the_address_space() -> None:
    with pytest.raises(ConfigError, match="slot_count"):
        validate(Config(dimm=DimmSpec(base_number=0xF0, slot_count=32)))
