"""Command line interface: status, set, verify, auto, run."""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from sr665_fanctl.config import Config, ConfigError, load_config, validate
from sr665_fanctl.controller import Controller
from sr665_fanctl.curve import Curve, interpolate
from sr665_fanctl.hpt import HptClient, HptError
from sr665_fanctl.ipmi import (
    MAX_SPEED_BYTE,
    MIN_SPEED_BYTE,
    DryRunClient,
    IpmiClient,
    IpmiError,
)
from sr665_fanctl.nvme import NvmeRefresher
from sr665_fanctl.sensors import HWMON_ROOT, read_nvme_temps, read_socket_temps
from sr665_fanctl.verify import conversion_problems

log = logging.getLogger(__name__)

#: Patched by tests; both take no arguments / the nvme spec respectively.
CLIENT_FACTORY = IpmiClient
HPT_FACTORY = HptClient
#: The cross-check compares two reads taken ~10 s apart. Right after a crash
#: (or at boot) the BMC may still be ramping the fans, so a mismatch can be a
#: transient; retry after the fans have settled before refusing to start.
VERIFY_RETRY_DELAY_S = 20.0
VERIFY_RETRIES = 2


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        return args.func(load_config(args.config), args)
    except (ConfigError, IpmiError, ValueError) as error:
        log.error("%s", error)
        return 1
    except KeyboardInterrupt:
        log.info("stopped by the user")
        return 0


def build_parser() -> argparse.ArgumentParser:
    """The fanctl argument parser: one subcommand per action."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="TOML config path")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    parser = argparse.ArgumentParser(prog="fanctl")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", parents=[common], help="show temperatures and fan speeds")
    status.add_argument("--nvme", action="store_true",
                        help="also query every NVMe drive (~3 s each, serialised by hptsvr)")
    status.set_defaults(func=_cmd_status)

    setter = sub.add_parser("set", parents=[common], help="send a fixed speed byte to the fans")
    setter.add_argument("duty", type=int, help=f"{MIN_SPEED_BYTE}..{MAX_SPEED_BYTE} (speed byte)")
    setter.set_defaults(func=_cmd_set)

    checker = sub.add_parser(
        "verify", parents=[common],
        help="cross-check the fast sensor path against the SDR reference",
    )
    checker.set_defaults(func=_cmd_verify)

    auto = sub.add_parser("auto", parents=[common], help="hand fan control back to the BMC")
    auto.set_defaults(func=_cmd_auto)

    runner = sub.add_parser("run", parents=[common], help="run the control loop")
    runner.add_argument("--dry-run", action="store_true", help="print writes instead of sending")
    runner.add_argument("--once", action="store_true", help="one cycle, then exit")
    runner.add_argument("--skip-verify", action="store_true",
                        help="start without the sensor cross-check (not recommended)")
    runner.set_defaults(func=_cmd_run)
    return parser


def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    """Print temperatures, fan speeds and the duty each curve would pick."""
    started = time.monotonic()
    client = CLIENT_FACTORY()
    try:
        slots = client.discover_dimm_slots(cfg.dimm)
        sensors = client.read_sensors(cfg.tach, cfg.dimm, slots)
    except IpmiError as error:
        log.error("IPMI read failed: %s", error)
        return 1
    elapsed = time.monotonic() - started

    cpu1, cpu2 = read_socket_temps(HWMON_ROOT)
    nvme = read_nvme_temps(HWMON_ROOT)
    drive_temps: dict[str, float] = {}
    if args.nvme and cfg.nvme.devices:
        try:
            drive_temps = {
                device: value
                for device, value in HptClient(cfg.nvme).read_all().items()
                if value is not None
            }
        except HptError as error:
            log.error("NVMe temperature query failed: %s", error)
    nvme_values = nvme + list(drive_temps.values())
    dimm = max(sensors.dimm_temps.values(), default=None)
    rows = [
        ("cpu1", cpu1, cfg.cpu_curve),
        ("cpu2", cpu2, cfg.cpu_curve),
        ("nvme", max(nvme_values, default=None), cfg.nvme_curve),
        ("dimm", dimm, cfg.dimm_curve),
    ]
    for name, value, curve in rows:
        print(f"{name:5} {_temp(value):>7}  curve→byte {_duty(value, curve)}")
    print(f"nvme drives: {len(nvme_values)}   dimm slots readable: {len(sensors.dimm_temps)}")
    if drive_temps:
        for device, value in drive_temps.items():
            print(f"nvme {device}: {value:.0f}C")
    elif cfg.nvme.devices and not args.nvme:
        print("nvme (HighPoint): not queried; use --nvme (~3 s per drive)")
    for fan in cfg.fans:
        speeds = sensors.tach.get(fan)
        front, rear = ("?", "?") if speeds is None else (str(speeds[0]), str(speeds[1]))
        print(f"fan {fan}: front {front:>5} rpm   rear {rear:>5} rpm")
    print(f"ipmi read took {elapsed:.2f}s")
    return 0


def _cmd_set(cfg: Config, args: argparse.Namespace) -> int:
    """Send a fixed speed byte, clamped to the measured hardware floor."""
    if not MIN_SPEED_BYTE <= args.duty <= MAX_SPEED_BYTE:
        log.error(f"duty must be {MIN_SPEED_BYTE}..{MAX_SPEED_BYTE} (0 means automatic)")
        return 2
    floor = max(cfg.hard_floor_duty, MIN_SPEED_BYTE)
    duty = max(args.duty, floor)
    if duty != args.duty:
        log.warning("raised %d to the measured floor %d", args.duty, duty)
    CLIENT_FACTORY().set_duty(duty)
    print(f"all fans: speed byte {duty}")
    return 0


def _cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    """Compare the fast raw reads with the SDR-decoded reference."""
    problems = _cross_check(cfg)
    if problems:
        for problem in problems:
            print(f"MISMATCH: {problem}")
        print("Refusing to trust the fast sensor path; see docs/probe-results.md.")
        return 1
    print("sensor paths agree: raw reads match the SDR reference")
    return 0


def _cmd_auto(cfg: Config, args: argparse.Namespace) -> int:
    """Hand fan control back to the BMC (verified by the Phase 0 probe)."""
    CLIENT_FACTORY().set_auto()
    print("fan control handed back to the BMC")
    return 0


def _cross_check(cfg: Config) -> list[str]:
    """Run the raw-vs-SDR cross-check, retrying while the fans may be ramping."""
    problems = conversion_problems(CLIENT_FACTORY(), cfg)
    for attempt in range(VERIFY_RETRIES):
        if not problems:
            return problems
        log.warning(
            "sensor cross-check mismatch (attempt %d/%d); the fans may still be "
            "ramping, retrying in %.0f s", attempt + 1, VERIFY_RETRIES + 1,
            VERIFY_RETRY_DELAY_S,
        )
        time.sleep(VERIFY_RETRY_DELAY_S)
        problems = conversion_problems(CLIENT_FACTORY(), cfg)
    return problems


def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    """Run the closed loop; refuses to start before the floor is measured."""
    cfg = validate(cfg, require_measured_floor=True)
    if not args.skip_verify:
        problems = _cross_check(cfg)
        if problems:
            for problem in problems:
                log.error("MISMATCH: %s", problem)
            log.error("sensor cross-check failed after %d attempts; refusing to start",
                      VERIFY_RETRIES + 1)
            return 1
    reader = CLIENT_FACTORY()
    client = DryRunClient(reader) if args.dry_run else reader
    refresher = None
    if cfg.nvme.devices:
        refresher = NvmeRefresher(HPT_FACTORY(cfg.nvme), cfg.nvme)
        if not args.once:
            refresher.start()
    controller = Controller(client, cfg, hwmon_root=HWMON_ROOT, nvme=refresher)
    try:
        if args.once:
            controller.run_cycle()
            return 0
        controller.run_forever()
        return 0
    finally:
        if refresher is not None:
            refresher.stop()


def _temp(value: float | None) -> str:
    """A temperature cell: ``39.0C`` or ``--`` when unreadable."""
    return "--" if value is None else f"{value:.1f}C"


def _duty(value: float | None, curve: Curve) -> str:
    """The duty a curve would pick for ``value``, or ``--``."""
    return "--" if value is None else str(interpolate(value, curve))
