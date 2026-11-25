"""Background refresh of the NVMe temperatures."""
import threading
import time

from sr665_fanctl.config import NvmeSpec
from sr665_fanctl.hpt import HptError
from sr665_fanctl.nvme import NvmeRefresher

SPEC = NvmeSpec(devices=("1/E1/1", "1/E1/2"), pause_s=0.0, stale_s=60.0)


class FakeHpt:
    """Answers with a fixed temperature per device; can also fail."""

    def __init__(self, values: dict[str, float], fail: bool = False) -> None:
        self.values = values
        self.fail = fail
        self.calls: list[str] = []

    def read_device(self, device: str) -> float | None:
        self.calls.append(device)
        if self.fail:
            raise HptError(f"{device}: rsp 0xcb")
        return self.values.get(device)


def test_polling_visits_every_device_in_turn() -> None:
    client = FakeHpt({"1/E1/1": 40.0, "1/E1/2": 35.0})
    refresher = NvmeRefresher(client, SPEC)
    assert refresher.poll_once() == "1/E1/1"
    assert refresher.poll_once() == "1/E1/2"
    assert refresher.poll_once() == "1/E1/1"
    assert client.calls == ["1/E1/1", "1/E1/2", "1/E1/1"]


def test_latest_returns_the_fresh_readings() -> None:
    client = FakeHpt({"1/E1/1": 40.0, "1/E1/2": 35.0})
    refresher = NvmeRefresher(client, SPEC)
    refresher.poll_once()
    refresher.poll_once()
    assert refresher.latest() == {"1/E1/1": 40.0, "1/E1/2": 35.0}


def test_stale_readings_drop_out() -> None:
    clock = {"now": 1000.0}
    client = FakeHpt({"1/E1/1": 40.0})
    refresher = NvmeRefresher(client, SPEC, clock=lambda: clock["now"])
    refresher.poll_once()
    assert refresher.latest() == {"1/E1/1": 40.0}
    clock["now"] += 61.0
    assert refresher.latest() == {}


def test_a_failed_query_keeps_the_last_value_and_records_the_error() -> None:
    clock = {"now": 1000.0}
    client = FakeHpt({"1/E1/1": 40.0})
    refresher = NvmeRefresher(client, SPEC, clock=lambda: clock["now"])
    refresher.poll_once()
    client.fail = True
    refresher.poll_once()
    assert refresher.last_error is not None
    assert refresher.latest() == {"1/E1/1": 40.0}  # the old reading is still fresh


def test_no_configured_devices_means_no_polling() -> None:
    refresher = NvmeRefresher(FakeHpt({}), NvmeSpec(devices=()))
    assert refresher.poll_once() is None
    assert refresher.latest() == {}


def test_the_background_thread_starts_and_stops() -> None:
    client = FakeHpt({"1/E1/1": 40.0, "1/E1/2": 35.0})
    refresher = NvmeRefresher(client, SPEC)
    refresher.start()
    deadline = time.monotonic() + 5.0
    while not refresher.latest() and time.monotonic() < deadline:
        time.sleep(0.01)
    refresher.stop()
    assert refresher.latest() != {}  # at least one probe landed
    assert isinstance(threading.current_thread(), threading.Thread)
