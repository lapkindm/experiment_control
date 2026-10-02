"""
Command-line entry point.

    python -m expctl config.toml              # GUI with real hardware
    python -m expctl config.toml --simulate   # GUI with simulated devices
    python -m expctl config.toml --no-gui     # log only, status on stdout
"""

from __future__ import annotations

import argparse
import logging
import math
import signal
import sys
import threading
from pathlib import Path

from .acquisition import Acquisition
from .config import Config, load_config
from .datalog import CsvLogger
from .samples import Sample
from .units import RATE_TO_DISPLAY, THICKNESS_TO_DISPLAY


def build_devices(config: Config):
    """
    Return (heaters, qcm, time_scale).
    """

    if config.simulation.enabled:
        from .simulation import SimulatedChamber

        chamber = SimulatedChamber(config.cells, config.simulation)
        return chamber.heaters, chamber.qcm, config.simulation.speed

    from .devices import EurothermHeater, SQM160Monitor

    heaters = [EurothermHeater(cell) for cell in config.cells]
    return heaters, SQM160Monitor(config.qcm), 1.0


def setup_logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    )
    root.addHandler(console)

    events = logging.FileHandler(directory / "events.log", encoding="utf-8")
    events.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(events)

    # The eurotherm driver logs every communication error with a traceback
    # before raising it; expctl reports these itself.
    logging.getLogger("eurotherm").setLevel(logging.CRITICAL)


def _status_line(config: Config, sample: Sample) -> str:
    def f(value, fmt):
        return "---" if not math.isfinite(value) else format(value + 0.0, fmt)

    parts = []
    for cell, values in zip(config.cells, sample.cells):
        parts.append(
            f"{cell.name}: T={f(values.temperature, '.1f')} °C "
            f"SP={f(values.target_setpoint, '.1f')} °C "
            f"rate={f(values.rate * RATE_TO_DISPLAY, '.2f')} Å/min "
            f"d={f(values.thickness * THICKNESS_TO_DISPLAY, '.0f')} Å"
        )
    return " | ".join(parts)


def run_gui(config, acquisition, data_logger) -> int:
    from PyQt6.QtWidgets import QApplication

    from .gui import Bridge, MainWindow, QtLogHandler

    app = QApplication(sys.argv)

    bridge = Bridge()
    handler = QtLogHandler(bridge)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)

    title = "Deposition control"
    if config.simulation.enabled:
        title += f"  [SIMULATION x{config.simulation.speed:g}]"

    window = MainWindow(config, acquisition, data_logger, bridge, title)
    acquisition.add_listener(bridge.sample.emit)

    acquisition.start()
    window.show()

    try:
        return app.exec()
    finally:
        logging.getLogger().removeHandler(handler)


def run_headless(config, acquisition, data_logger) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    if not data_logger.active:
        logging.warning("Running without GUI and without data logging.")

    acquisition.add_listener(lambda s: print(_status_line(config, s), flush=True))
    acquisition.start()
    stop.wait()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="expctl",
        description="Effusion cell temperature / deposition rate control.",
    )
    parser.add_argument("config", type=Path, help="TOML configuration file")
    parser.add_argument(
        "--simulate", action="store_true",
        help="use simulated devices instead of the hardware",
    )
    parser.add_argument(
        "--speed", type=float,
        help="simulation speed factor (implies --simulate)",
    )
    parser.add_argument(
        "--failure-rate", type=float,
        help="simulation: probability that a request gets no answer "
             "(implies --simulate)",
    )
    parser.add_argument(
        "--no-gui", action="store_true",
        help="run without the GUI, print status lines to stdout",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.simulate or args.speed is not None or args.failure_rate is not None:
        config.simulation.enabled = True
    if args.speed is not None:
        config.simulation.speed = args.speed
    if args.failure_rate is not None:
        config.simulation.failure_rate = args.failure_rate

    setup_logging(config.log_directory)

    heaters, qcm, time_scale = build_devices(config)
    data_logger = CsvLogger(
        config.log_directory, [cell.key for cell in config.cells]
    )
    acquisition = Acquisition(config, heaters, qcm, data_logger, time_scale)

    if config.log_autostart:
        path = data_logger.start()
        logging.info("Logging to %s", path)

    try:
        if args.no_gui:
            return run_headless(config, acquisition, data_logger)
        return run_gui(config, acquisition, data_logger)
    finally:
        acquisition.stop()
        data_logger.stop()


if __name__ == "__main__":
    sys.exit(main())
