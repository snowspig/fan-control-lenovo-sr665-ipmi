"""Background refresh of the NVMe drive temperatures.

One ``hptraidconf`` query costs about 3 s and ``hptsvr`` serves them one at a
time, so the temperatures are collected in a daemon thread, one drive per
round. The control loop only ever looks at ``latest()``, which never blocks:
it returns the readings that are still fresh and drops the rest.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from sr665_fanctl.config import NvmeSpec
from sr665_fanctl.hpt import HptClient, HptError

log = logging.getLogger(__name__)


class NvmeRefresher:
    """Keeps the newest temperature of every NVMe drive, one probe at a time."""

    def __init__(
        self, client: HptClient, spec: NvmeSpec,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.spec = spec
        self._clock = clock
        self._readings: dict[str, tuple[float, float]] = {}  # device -> (temp, time)
        self._next = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def poll_once(self) -> str | None:
        """Query the next drive in the round; returns its id, or None if none are configured."""
        if not self.spec.devices:
            return None
        device = self.spec.devices[self._next % len(self.spec.devices)]
        self._next += 1
        try:
            value = self.client.read_device(device)
        except HptError as error:
            self.last_error = str(error)
            log.warning("nvme temperature query failed: %s", error)
            return device
        self.last_error = None
        if value is not None:
            self._readings[device] = (value, self._clock())
        return device

    def latest(self) -> dict[str, float]:
        """Fresh readings only: device -> degrees C, oldest entry dropped first."""
        now = self._clock()
        return {
            device: value
            for device, (value, seen) in sorted(self._readings.items())
            if now - seen <= self.spec.stale_s
        }

    def start(self) -> None:
        """Begin probing in a daemon thread; safe to call once."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="nvme-temps", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the background thread to exit after its current probe."""
        self._stop.set()

    def _loop(self) -> None:
        """Probe drives forever, pausing ``pause_s`` between probes."""
        while True:
            self.poll_once()
            if self._stop.wait(self.spec.pause_s):
                return
