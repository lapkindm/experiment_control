import csv
import threading
from pathlib import Path

import pytest

from expctl.acquisition import Acquisition
from expctl.config import (
    CellConfig, Config, RateConfig, SimulationConfig, load_config,
)
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
        rate=RateConfig(window=0.2),
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


class CountingHeater(FlakyHeater):
    """
    Fails the next ``fail_reads`` reads / ``fail_writes`` setpoint writes.
    """

    def __init__(self, heater):
        super().__init__(heater)
        self.fail_reads = 0
        self.fail_writes = 0
        self.writes = []

    def read(self):
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise TimeoutError("no response")
        return self.heater.read()

    def set_setpoint(self, value):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise TimeoutError("no response")
        self.writes.append(value)
        self.heater.set_setpoint(value)


def test_single_miss_keeps_device_open(tmp_path):
    config = make_config(tmp_path)
    chamber = SimulatedChamber(config.cells, config.simulation, seed=0)
    heater = CountingHeater(chamber.heaters[0])
    acq = Acquisition(config, [heater, chamber.heaters[1]], chamber.qcm)
    collect = Collector()
    acq.add_listener(collect)

    acq.start()
    try:
        collect.wait_for(lambda s: len(s) >= 2)

        # Two failures: recovered by the retries, no value lost.
        heater.fail_reads = 2
        collect.wait_for(lambda s: heater.fail_reads == 0 and len(s) >= 5)

        # One request with all three attempts failing: one value missing.
        heater.fail_reads = 3
        n = len(collect.samples)
        collect.wait_for(lambda s: len(s) >= n + 5)
    finally:
        acq.stop()

    temperatures = [s.cells[0].temperature for s in collect.samples]
    assert sum(t != t for t in temperatures) == 1
    assert heater.opens == 1


def test_unacknowledged_setpoint_is_retried(tmp_path):
    config = make_config(tmp_path)
    chamber = SimulatedChamber(config.cells, config.simulation, seed=0)
    heater = CountingHeater(chamber.heaters[0])
    acq = Acquisition(config, [heater, chamber.heaters[1]], chamber.qcm)
    collect = Collector()
    acq.add_listener(collect)

    acq.start()
    try:
        collect.wait_for(lambda s: len(s) >= 2)

        # Fails for the first request (3 attempts) and the first retry
        # of the next cycle.
        heater.fail_writes = 4
        acq.set_setpoint(0, 300.0)
        collect.wait_for(lambda s: s[-1].cells[0].target_setpoint == 300.0)
    finally:
        acq.stop()

    assert heater.writes == [300.0]
    assert heater.opens == 1


def test_feedback_survives_sporadic_failures(tmp_path):
    config = make_config(tmp_path, interval=0.01)
    config.simulation.failure_rate = 0.3
    chamber = SimulatedChamber(config.cells, config.simulation, seed=3)
    acq = Acquisition(config, chamber.heaters, chamber.qcm)
    collect = Collector()
    acq.add_listener(collect)

    acq.start()
    try:
        collect.wait_for(
            lambda s: len(s) >= 3 and s[-1].cells[1].target_setpoint == 25.0
        )
        acq.set_setpoint(1, 400.0)
        collect.wait_for(lambda s: s[-1].cells[1].target_setpoint == 400.0)
        acq.enable_feedback(1, 1.0)
        collect.wait_for(lambda s: s[-1].cells[1].feedback)
        n = len(collect.samples)
        collect.wait_for(lambda s: len(s) >= n + 300, timeout=20)
    finally:
        acq.stop()

    after = collect.samples[n:]
    assert all(s.cells[1].feedback for s in after)
    missing = sum(s.cells[1].temperature != s.cells[1].temperature for s in after)
    # 0.3**3 = 2.7 % of the requests are lost despite the retries.
    assert 0 < missing < 0.08 * len(after)
    # The rate is ~0 at 400 °C, so the feedback keeps raising the setpoint.
    assert after[-1].cells[1].target_setpoint > 400.0


class ThreadRecorder:
    """
    Wraps a device, records which thread each call runs on; optionally slow.
    """

    def __init__(self, device, delay=0.0):
        self.device = device
        self.name = device.name
        self.delay = delay
        self.threads = set()

    def _record(self):
        self.threads.add(threading.current_thread().name)
        if self.delay:
            import time
            time.sleep(self.delay)

    def open(self):
        self.threads.add(threading.current_thread().name)
        self.device.open()

    def close(self):
        self.threads.add(threading.current_thread().name)
        self.device.close()

    def read(self):
        self._record()
        return self.device.read()

    def set_setpoint(self, value):
        self._record()
        self.device.set_setpoint(value)


def test_each_device_on_its_own_thread(tmp_path):
    config = make_config(tmp_path)
    chamber = SimulatedChamber(config.cells, config.simulation, seed=0)
    devices = [ThreadRecorder(d) for d in [*chamber.heaters, chamber.qcm]]
    acq = Acquisition(config, devices[:2], devices[2])
    collect = Collector()
    acq.add_listener(collect)

    acq.start()
    try:
        collect.wait_for(lambda s: len(s) >= 3)
        acq.set_setpoint(0, 300.0)
        acq.set_setpoint(1, 200.0)
        collect.wait_for(
            lambda s: s[-1].cells[0].target_setpoint == 300.0
            and s[-1].cells[1].target_setpoint == 200.0
        )
    finally:
        acq.stop()

    # One thread per device (including open/close and writes), all
    # different, none of them the acquisition thread.
    threads = [d.threads for d in devices]
    assert all(len(t) == 1 for t in threads)
    assert len(set.union(*threads)) == 3
    assert "acquisition" not in set.union(*threads)


def test_slow_device_does_not_delay_others(tmp_path):
    interval = 0.05
    config = make_config(tmp_path, interval=interval)
    chamber = SimulatedChamber(config.cells, config.simulation, seed=0)
    slow = ThreadRecorder(chamber.heaters[0], delay=6 * interval)
    acq = Acquisition(config, [slow, chamber.heaters[1]], chamber.qcm)
    collect = Collector()
    acq.add_listener(collect)

    acq.start()
    try:
        collect.wait_for(lambda s: len(s) >= 40, timeout=10)
    finally:
        acq.stop()

    samples = collect.samples[1:]
    period = (samples[-1].elapsed - samples[0].elapsed) / (len(samples) - 1)
    assert period == pytest.approx(interval, rel=0.2)

    def present(values):
        return sum(v == v for v in values)

    # The other devices answer every time, the slow one now and then.
    assert present(s.cells[1].temperature for s in samples) == len(samples)
    assert present(s.cells[1].thickness for s in samples) == len(samples)
    assert 0 < present(s.cells[0].temperature for s in samples) < len(samples) / 3
