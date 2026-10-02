import csv
import threading
from pathlib import Path

import pytest

from expctl.acquisition import Acquisition
from expctl.config import CellConfig, Config, SimulationConfig, load_config
from expctl.datalog import CsvLogger, columns
from expctl.simulation import SimulatedChamber

ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path, interval=0.02):
    cells = [
        CellConfig(name="Cell A", key="A", sensor=1, model="2408", port="sim",
                   min_setpoint=20, max_setpoint=900),
        CellConfig(name="Cell B", key="B", sensor=2, model="3508", port="sim",
                   min_setpoint=20, max_setpoint=600),
    ]
    return Config(
        interval=interval,
        reconnect_delay=0.05,
        log_directory=tmp_path,
        cells=cells,
        simulation=SimulationConfig(enabled=True, speed=1.0),
    )


class Collector:
    def __init__(self):
        self.samples = []
        self._cond = threading.Condition()

    def __call__(self, sample):
        with self._cond:
            self.samples.append(sample)
            self._cond.notify_all()

    def wait_for(self, predicate, timeout=5.0):
        with self._cond:
            ok = self._cond.wait_for(lambda: predicate(self.samples), timeout)
        assert ok, "condition not reached"
        return self.samples[-1]


def test_example_config_loads():
    config = load_config(ROOT / "config.example.toml")
    assert [c.key for c in config.cells] == ["A", "B"]
    assert config.cells[1].max_setpoint == 800.0
    assert config.log_directory == ROOT / "logs"


def test_setpoint_feedback_and_logging(tmp_path):
    config = make_config(tmp_path)
    chamber = SimulatedChamber(config.cells, config.simulation, seed=0)
    logger = CsvLogger(tmp_path, ["A", "B"])
    acq = Acquisition(config, chamber.heaters, chamber.qcm, logger)
    collect = Collector()
    acq.add_listener(collect)

    path = logger.start()
    acq.start()
    try:
        collect.wait_for(lambda s: len(s) >= 3)

        # Manual setpoint, clamped to the cell limit.
        acq.set_setpoint(1, 5000.0)
        last = collect.wait_for(lambda s: s[-1].cells[1].target_setpoint == 600.0)
        assert last.cells[0].target_setpoint == 25.0

        # Feedback on: the cell is still cold, the rate ~0, so the
        # feedback raises the setpoint.
        acq.set_setpoint(1, 400.0)
        collect.wait_for(lambda s: s[-1].cells[1].target_setpoint == 400.0)
        acq.enable_feedback(1, 1.0)
        collect.wait_for(lambda s: s[-1].cells[1].feedback)
        collect.wait_for(lambda s: s[-1].cells[1].target_setpoint > 400.0)

        # A manual setpoint switches the feedback off.
        acq.set_setpoint(1, 300.0)
        last = collect.wait_for(
            lambda s: not s[-1].cells[1].feedback
            and s[-1].cells[1].target_setpoint == 300.0
        )
    finally:
        acq.stop()
        logger.stop()

    with path.open() as f:
        rows = list(csv.reader(f))

    assert rows[0] == columns(["A", "B"])
    assert len(rows) > 5
    assert all(len(row) == len(rows[0]) for row in rows)


class FlakyHeater:
    """
    Wraps a simulated heater and fails while ``broken`` is set.
    """

    def __init__(self, heater):
        self.heater = heater
        self.name = heater.name
        self.broken = False
        self.opens = 0

    def open(self):
        if self.broken:
            raise ConnectionError("port gone")
        self.opens += 1

    def close(self):
        pass

    def read(self):
        if self.broken:
            raise TimeoutError("no response")
        return self.heater.read()

    def set_setpoint(self, value):
        if self.broken:
            raise TimeoutError("no response")
        self.heater.set_setpoint(value)


def test_device_failure_and_recovery(tmp_path):
    config = make_config(tmp_path)
    chamber = SimulatedChamber(config.cells, config.simulation, seed=0)
    flaky = FlakyHeater(chamber.heaters[0])
    acq = Acquisition(config, [flaky, chamber.heaters[1]], chamber.qcm)
    collect = Collector()
    acq.add_listener(collect)

    acq.start()
    try:
        collect.wait_for(lambda s: len(s) >= 2)

        acq.enable_feedback(0, 1.0)
        collect.wait_for(lambda s: s[-1].cells[0].feedback)

        flaky.broken = True
        last = collect.wait_for(
            lambda s: s[-1].cells[0].temperature != s[-1].cells[0].temperature
        )
        # The other cell keeps working.
        assert last.cells[1].temperature == pytest.approx(25.0)
        # Feedback cannot write the setpoint any more and switches off.
        collect.wait_for(lambda s: not s[-1].cells[0].feedback)

        flaky.broken = False
        collect.wait_for(lambda s: s[-1].cells[0].temperature == 25.0)
        assert flaky.opens == 2
    finally:
        acq.stop()
