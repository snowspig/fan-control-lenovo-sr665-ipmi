"""Read NVMe drive temperatures from HighPoint's ``hptsvr`` via ``hptraidconf``.

With the HighPoint driver (``hptnvme``) the kernel no longer exposes the
drives as ``nvme`` hwmon chips, so their SMART temperatures are only available
from the RAID management daemon. ``hptraidconf`` must be called in single-shot
form (the subcommand on the argv): feeding commands to its interactive mode
makes it loop forever on EOF and flood stdout. One query takes about 3 s and
``hptsvr`` serialises them, so the controller refreshes drives one at a time in
the background (see ``nvme.NvmeRefresher``).
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from sr665_fanctl.config import NvmeSpec

#: One summary line per query; anything larger means the tool misbehaved.
MAX_OUTPUT_BYTES = 1 << 20
#: Temperatures outside this range cannot be a real drive reading.
MIN_TEMP_C = 0
MAX_TEMP_C = 125

Runner = Callable[[list[str], float], str]

_TEMPERATURE_RE = re.compile(r"^Temperature\(C\):\s*(\d+)", re.MULTILINE)


class HptError(RuntimeError):
    """hptraidconf is missing, timed out, misbehaved, or reported an error."""


def build_query_args(spec: NvmeSpec, device: str) -> list[str]:
    """The argv of one single-shot device query."""
    return [spec.binary, "-u", spec.user, "-p", spec.password, "query", "devices", device]


def parse_temperature(text: str) -> float | None:
    """The drive temperature from one ``query devices <id>`` report."""
    match = _TEMPERATURE_RE.search(text)
    if match is None:
        return None
    value = float(match.group(1))
    return value if MIN_TEMP_C <= value <= MAX_TEMP_C else None


def _run(args: list[str], timeout: float) -> str:
    """Run one hptraidconf command; never touches stdin, always bounded."""
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as error:
        raise HptError(f"{args[0]} not installed: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise HptError(f"{args[-1]}: {' '.join(args[:2])} timed out after {timeout}s") from error
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        raise HptError(f"{args[-1]}: output exceeded {MAX_OUTPUT_BYTES} bytes")
    if result.returncode != 0 or parse_temperature(result.stdout) is None:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise HptError(f"{args[-1]}: {detail[-1] if detail else 'no temperature in output'}")
    return result.stdout


class HptClient:
    """Queries one drive at a time; ``runner`` is injectable for tests."""

    def __init__(self, spec: NvmeSpec, runner: Runner | None = None) -> None:
        self.spec = spec
        self._runner = runner if runner is not None else _run

    def read_device(self, device: str) -> float | None:
        """Composite temperature of one drive in degrees C; None when absent."""
        return parse_temperature(
            self._runner(build_query_args(self.spec, device), self.spec.timeout_s)
        )

    def read_all(self) -> dict[str, float | None]:
        """Every configured device, queried one after another (about 3 s each)."""
        return {device: self.read_device(device) for device in self.spec.devices}
