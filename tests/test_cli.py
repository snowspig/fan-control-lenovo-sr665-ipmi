"""CLI wiring: status / set / verify / run argument handling."""
import pytest

from sr665_fanctl import cli
from sr665_fanctl.ipmi import IpmiSensors


class RecordingClient:
    """Stands in for IpmiClient and records every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def set_duty(self, duty: int) -> None:
        self.writes.append(duty)

    def set_auto(self) -> None:
        self.writes.append("auto")

    def discover_dimm_slots(self, spec) -> list[int]:
        return [1]

    def read_sensors(self, tach, dimm, dimm_slots) -> IpmiSensors:
        return IpmiSensors(tach={1: (8560, 8510)}, dimm_temps={1: 31.0})


@pytest.fixture
def recording_client(monkeypatch) -> RecordingClient:
    """Replace the real BMC client with a recorder."""
    client = RecordingClient()
    monkeypatch.setattr(cli, "CLIENT_FACTORY", lambda: client)
    return client


def test_set_clamps_to_the_measured_floor(tmp_path, recording_client) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("hard_floor_duty = 20\n", encoding="utf-8")  # must stay <= min_duty
    assert cli.main(["set", "10", "--config", str(cfg_path)]) == 0
    assert recording_client.writes == [20]


def test_set_rejects_the_reserved_zero_byte(recording_client) -> None:
    assert cli.main(["set", "0"]) == 2
    assert recording_client.writes == []


def test_set_sends_the_speed_byte(recording_client) -> None:
    assert cli.main(["set", "40"]) == 0
    assert recording_client.writes == [40]


def test_status_prints_a_report(recording_client, capsys, hwmon_root, monkeypatch) -> None:
    monkeypatch.setattr(cli, "HWMON_ROOT", hwmon_root)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "nvme drives: 3" in out
    assert "fan 1: front  8560 rpm" in out
    assert "ipmi read took" in out


def test_run_refuses_without_a_measured_floor() -> None:
    assert cli.main(["run", "--once"]) == 1


def test_run_refuses_when_the_sensor_paths_disagree(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "VERIFY_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(cli, "conversion_problems",
                        lambda client, cfg: ["fan 1 rear: raw path 9600 rpm, reference 8880 rpm"])
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("hard_floor_duty = 20\n", encoding="utf-8")
    assert cli.main(["run", "--once", "--config", str(cfg_path)]) == 1


def test_run_retries_a_transient_mismatch_and_starts(
    tmp_path, recording_client, monkeypatch
) -> None:
    """After a crash the fans are still ramping; the first check may disagree."""
    monkeypatch.setattr(cli, "VERIFY_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(cli, "HWMON_ROOT", tmp_path / "no-hwmon")
    checks: list[int] = []

    def flaky(client, cfg):
        checks.append(1)
        # The sensor paths disagree once, then agree once the fans have settled.
        return ["fan 1 front: raw path 8880 rpm, reference 8000 rpm"] if len(checks) == 1 else []

    monkeypatch.setattr(cli, "conversion_problems", flaky)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("hard_floor_duty = 20\n", encoding="utf-8")
    assert cli.main(["run", "--once", "--dry-run", "--config", str(cfg_path)]) == 0
    assert len(checks) == 2


def test_run_gives_up_after_the_last_attempt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "VERIFY_RETRY_DELAY_S", 0.0)
    checks: list[int] = []
    monkeypatch.setattr(
        cli, "conversion_problems",
        lambda client, cfg: checks.append(1) or ["still wrong"],
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("hard_floor_duty = 20\n", encoding="utf-8")
    assert cli.main(["run", "--once", "--config", str(cfg_path)]) == 1
    assert len(checks) == cli.VERIFY_RETRIES + 1


def test_verify_reports_agreement(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "VERIFY_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(cli, "conversion_problems", lambda client, cfg: [])
    assert cli.main(["verify"]) == 0
    assert "sensor paths agree" in capsys.readouterr().out


def test_verify_reports_mismatches_with_exit_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "VERIFY_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(cli, "conversion_problems",
                        lambda client, cfg: ["dimm 16: raw path -40 C, reference 31.0 C"])
    assert cli.main(["verify"]) == 1
    assert "MISMATCH: dimm 16" in capsys.readouterr().out


def test_auto_hands_control_back_to_the_bmc(recording_client) -> None:
    assert cli.main(["auto"]) == 0
    assert recording_client.writes == ["auto"]


def test_run_once_dry_run_prints_instead_of_sending(
    hwmon_root, tmp_path, capsys, recording_client, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "HWMON_ROOT", hwmon_root)
    monkeypatch.setattr(cli, "conversion_problems", lambda client, cfg: [])
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("hard_floor_duty = 20\n", encoding="utf-8")
    assert cli.main(["run", "--once", "--dry-run", "--config", str(cfg_path)]) == 0
    assert "[dry-run] ipmitool raw 0x3a 0x07 0xff 0x54 0x01" in capsys.readouterr().out
    assert recording_client.writes == []
