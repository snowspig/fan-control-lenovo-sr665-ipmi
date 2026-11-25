"""The control loop: read temperatures, verify the last write, decide, write."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from sr665_fanctl.config import Config
from sr665_fanctl.curve import Decision, State, Temps, decide, tach_faults
from sr665_fanctl.ipmi import IpmiClient, IpmiError, IpmiSensors
from sr665_fanctl.nvme import NvmeRefresher
from sr665_fanctl.sensors import HWMON_ROOT, read_temps

log = logging.getLogger(__name__)

Sleep = Callable[[float], None]
#: Reasons that must be logged at INFO because they are safety actions.
LOUD_REASONS = ("emergency", "blind", "tach")


class Controller:
    """Owns the state machine and performs one fan write per cycle."""

    def __init__(
        self, client: IpmiClient, cfg: Config, hwmon_root: Path = HWMON_ROOT,
        sleep: Sleep | None = None, nvme: NvmeRefresher | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.hwmon_root = hwmon_root
        self._sleep = sleep if sleep is not None else time.sleep
        self.nvme = nvme
        self.state = State(duty=cfg.safe_duty)
        self._dimm_slots: list[int] | None = None

    def run_cycle(self) -> Decision:
        """One iteration: read → verify the previous write → decide → write."""
        raw = self._read_sensors()
        nvme_temps = self.nvme.latest() if self.nvme is not None else {}
        temps = read_temps(self.hwmon_root, raw, nvme_temps)
        state = self._check_baselines(self.state, temps)
        if state.sent:
            state = self._verify(state, raw.tach)
        previous = state.duty
        decision, state = decide(state, temps, self.cfg)
        self._log_decision(previous, decision)
        self._write(decision.duty)
        self.state = replace(state, sent=True)
        return decision

    def run_forever(self) -> None:
        """Run cycles forever, pacing them to ``interval_s``."""
        while True:
            started = time.monotonic()
            self.run_cycle()
            elapsed = time.monotonic() - started
            self._sleep(max(0.0, self.cfg.interval_s - elapsed))

    def _read_sensors(self) -> IpmiSensors:
        """Read the BMC once; a failed read counts as unreadable data, not fatal."""
        try:
            if self._dimm_slots is None:
                slots = self.client.discover_dimm_slots(self.cfg.dimm)
                if slots:
                    self._dimm_slots = slots
                else:
                    log.warning("no DIMM sensor answered; retrying discovery next cycle")
            return self.client.read_sensors(
                self.cfg.tach, self.cfg.dimm, self._dimm_slots or []
            )
        except IpmiError as error:
            log.warning("IPMI read failed: %s", error)
            return IpmiSensors(tach={}, dimm_temps={})

    def _verify(self, state: State, tach: dict[int, tuple[int, int]]) -> State:
        """Check the previously written duty against the measured fan speeds."""
        faults = tach_faults(tach, state.duty, self.cfg)
        if not faults:
            if state.forced_full:
                log.info("tach healthy again, releasing the full-speed latch")
            return replace(state, tach_faults=0, forced_full=False)
        count = state.tach_faults + 1
        log.warning("tach check %d/%d failed: %s",
                    count, self.cfg.tach_fail_cycles, "; ".join(faults))
        return replace(state, tach_faults=count,
                       forced_full=count >= self.cfg.tach_fail_cycles)

    def _write(self, duty: int) -> None:
        """Send the speed, retrying once; the second failure is fatal."""
        try:
            self.client.set_duty(duty)
        except IpmiError as error:
            log.warning("fan write failed (%s), retrying once", error)
            self.client.set_duty(duty)

    def _check_baselines(self, state: State, temps: Temps) -> State:
        """Learn how many sensors exist, then warn when one disappears.

        The HighPoint refresh arrives one drive at a time, so the NVMe count is
        only trusted once every configured drive has reported at least once.
        """
        counts = {"nvme": temps.nvme_count, "dimm": temps.dimm_count}
        if state.baseline is None:
            expected_nvme = len(self.cfg.nvme.devices) if self.nvme is not None else 0
            if all(counts.values()) and counts["nvme"] >= expected_nvme:
                log.info("sensor baselines: %s", counts)
                return replace(state, baseline=dict(counts))
            return state
        for name, count in counts.items():
            known = state.baseline[name]
            if 0 < count < known:
                log.warning("%s sensor count dropped: %d -> %d", name, known, count)
        return state

    def _log_decision(self, previous: int, decision: Decision) -> None:
        """Duty changes and safety actions are INFO; steady state is DEBUG."""
        if decision.duty != previous or decision.reason.split()[0] in LOUD_REASONS:
            log.info("duty %d -> %d (%s)", previous, decision.duty, decision.reason)
        else:
            log.debug("duty %d (%s)", decision.duty, decision.reason)
