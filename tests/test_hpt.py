"""HighPoint CLI queries: argv shape and temperature parsing."""
import pytest

from sr665_fanctl.config import NvmeSpec
from sr665_fanctl.hpt import (
    HptClient,
    HptError,
    build_query_args,
    parse_temperature,
)


def test_build_query_args_is_single_shot() -> None:
    spec = NvmeSpec()
    assert build_query_args(spec, "1/E1/3") == [
        "hptraidconf", "-u", "RAID", "-p", "hpt", "query", "devices", "1/E1/3",
    ]


def test_parse_temperature_reads_the_summary_line(hpt_query_text: str) -> None:
    assert parse_temperature(hpt_query_text) == 38.0


def test_parse_temperature_is_none_without_a_reading() -> None:
    assert parse_temperature("Mode Number: nothing\n") is None
    assert parse_temperature("Temperature(C): 200\n") is None  # implausible


def test_read_device_uses_the_runner() -> None:
    seen: list[list[str]] = []

    def runner(args, timeout) -> str:
        seen.append(args)
        return "Temperature(C): 41                  Total Bytes Written: 1 TB\n"

    client = HptClient(NvmeSpec(), runner=runner)
    assert client.read_device("1/E1/1") == 41.0
    assert seen[0][-1] == "1/E1/1"


def test_read_all_covers_every_configured_device() -> None:
    def runner(args, timeout) -> str:
        return f"Temperature(C): {len(args[-1])}\n"

    client = HptClient(NvmeSpec(devices=("1/E1/1", "1/E1/2")), runner=runner)
    assert client.read_all() == {"1/E1/1": 6.0, "1/E1/2": 6.0}  # len("1/E1/1")


def test_a_failed_query_raises_hpt_error() -> None:
    def runner(args, timeout) -> str:
        raise HptError("1/E1/1: rsp 0xcb")

    client = HptClient(NvmeSpec(), runner=runner)
    with pytest.raises(HptError, match="rsp 0xcb"):
        client.read_device("1/E1/1")
