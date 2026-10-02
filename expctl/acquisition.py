"""
Acquisition loop.

A single background thread owns all instruments. It polls them at a fixed
interval, runs the rate feedback, writes the log and notifies listeners.
Requests from the GUI (setpoint changes, feedback on/off) are passed
through a queue and executed by the same thread, so no two threads ever
talk to the same serial port.

Devices that fail are closed and reopened after ``reconnect_delay``; in
the meantime their values are NaN and the remaining devices keep working.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import replace
from typing import Callable

from .config import Config
from .control import RateFeedback
from .datalog import CsvLogger
from .devices import Heater, HeaterReading, QCM, QCMChannel
from .rate import ThicknessRate
from .samples import CellSample, Sample

log = logging.getLogger(__name__)

_STOP = object()


class _Link:
    """
    Connection state of one device.
    """

    def __init__(self, device, reconnect_delay: float) -> None:
        self.device = device
        self.reconnect_delay = reconnect_delay
        self.connected = False
        self._next_attempt = 0.0

    def call(self, fn: Callable):
        """
        Run ``fn(device)``; return None if the device is unavailable.
        """

        if not self.connected:
            if time.monotonic() < self._next_attempt:
                return None
            try:
                self.device.open()
            except Exception as exc:
                self._fail(f"cannot connect: {exc}")
                return None
            self.connected = True
            log.info("%s connected.", self.device.name)

        try:
            return fn(self.device)
        except Exception as exc:
            self._fail(f"communication error: {exc}")
            return None

    def close(self) -> None:
        if self.connected:
            try:
                self.device.close()
            except Exception:
                log.exception("Error while closing %s.", self.device.name)
        self.connected = False

    def _fail(self, message: str) -> None:
        # Only report the first failure, not every reconnection attempt.
        if self.connected or self._next_attempt == 0.0:
            log.error("%s: %s", self.device.name, message)
        self.close()
        self._next_attempt = time.monotonic() + self.reconnect_delay


class Acquisition:
    """
    Parameters
    ----------
    config
        Program configuration.
    heaters
        One heater per configured cell, in the same order.
    qcm
        The deposition monitor.
    data_logger
        CSV logger receiving every sample.
    time_scale
        Simulated seconds per real second (only for the simulator). The
        polling interval, rate fit and feedback then run in simulated time.
    """

    def __init__(
        self,
        config: Config,
        heaters: list[Heater],
        qcm: QCM,
        data_logger: CsvLogger | None = None,
        time_scale: float = 1.0,
    ) -> None:
        if len(heaters) != len(config.cells):
            raise ValueError("Need exactly one heater per cell.")

        self.config = config
        self.data_logger = data_logger
        self.time_scale = time_scale

        self._heaters = [_Link(h, config.reconnect_delay) for h in heaters]
        self._qcm = _Link(qcm, config.reconnect_delay)

        self._feedback = [
            RateFeedback(c.feedback, c.min_setpoint, c.max_setpoint)
            for c in config.cells
        ]
        self._rate = [ThicknessRate(config.rate) for _ in config.cells]
        self._feedback_on = [False] * len(config.cells)
        self._written_setpoint = [math.nan] * len(config.cells)

        self._listeners: list[Callable[[Sample], None]] = []
        self._commands: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._last: Sample | None = None

    # ------------------------------------------------------------------
    # Public interface (thread-safe)
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[Sample], None]) -> None:
        """
        Register a callback; it is called from the acquisition thread.
        """
        self._listeners.append(callback)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Acquisition already running.")
        self._thread = threading.Thread(
            target=self._run, name="acquisition", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._commands.put(_STOP)
        self._thread.join(timeout)
        self._thread = None

    def set_setpoint(self, cell: int, value: float) -> None:
        """
        Write a temperature setpoint. Switches rate feedback off.
        """
        self._commands.put((self._do_set_setpoint, cell, value))

    def enable_feedback(self, cell: int, target_rate: float) -> None:
        """
        Start (or retarget) rate feedback for a cell.
        """
        self._commands.put((self._do_enable_feedback, cell, target_rate))

    def disable_feedback(self, cell: int) -> None:
        self._commands.put((self._do_disable_feedback, cell))

    # ------------------------------------------------------------------
    # Thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        interval = self.config.interval / self.time_scale
        t0 = time.monotonic()
        next_time = t0
        previous = None

        log.info("Acquisition started.")

        try:
            while True:
                now = time.monotonic()
                sample = self._acquire(now - t0, (now - t0) * self.time_scale)

                if previous is not None:
                    self._run_feedback(sample, (now - previous) * self.time_scale)
                previous = now

                sample = self._annotate(sample)
                self._last = sample

                if self.data_logger is not None:
                    try:
                        self.data_logger.write(sample)
                    except Exception:
                        log.exception("Writing the data log failed.")

                for listener in self._listeners:
                    try:
                        listener(sample)
                    except Exception:
                        log.exception("Sample listener failed.")

                next_time += interval
                if next_time < time.monotonic():
                    # Fell behind (slow devices): do not try to catch up.
                    next_time = time.monotonic() + interval

                if not self._process_commands(until=next_time):
                    break

        except Exception:
            log.exception("Acquisition loop crashed.")

        finally:
            for link in [*self._heaters, self._qcm]:
                link.close()
            log.info("Acquisition stopped.")

    def _process_commands(self, until: float) -> bool:
        """
        Execute queued commands until ``until``. False means stop.
        """

        while True:
            remaining = until - time.monotonic()
            try:
                command = self._commands.get(timeout=max(0.0, remaining))
            except queue.Empty:
                return True

            if command is _STOP:
                return False

            fn, *args = command
            try:
                fn(*args)
            except Exception:
                log.exception("Command %s%r failed.", fn.__name__, tuple(args))

    def _acquire(self, elapsed: float, process_time: float) -> Sample:
        """
        ``process_time`` is ``elapsed`` in simulated time (equal to it on
        the real hardware); the rate fit uses it.
        """

        timestamp = time.time()

        qcm: dict[int, QCMChannel] = (
            self._qcm.call(lambda d: d.read()) or {}
        )

        cells = []
        for cell, link, rate in zip(
            self.config.cells, self._heaters, self._rate
        ):
            heater: HeaterReading | None = link.call(lambda d: d.read())
            channel = qcm.get(cell.sensor)

            values = {}
            if heater is not None:
                values.update(heater._asdict())

            thickness = math.nan
            if channel is not None:
                thickness = channel.thickness
                values.update(
                    qcm_rate=channel.rate,
                    thickness=channel.thickness,
                    frequency=channel.frequency,
                )
            values["rate"] = rate.update(process_time, thickness)

            cells.append(CellSample(**values))

        return Sample(timestamp=timestamp, elapsed=elapsed, cells=cells)

    def _annotate(self, sample: Sample) -> Sample:
        cells = [
            replace(cell, feedback=on, rate_target=fb.target if on else math.nan)
            for cell, on, fb in zip(
                sample.cells, self._feedback_on, self._feedback
            )
        ]
        return replace(sample, cells=cells)

    # ------------------------------------------------------------------
    # Setpoints and feedback (acquisition thread only)
    # ------------------------------------------------------------------

    def _write_setpoint(self, index: int, value: float) -> bool:
        cell = self.config.cells[index]

        clamped = max(cell.min_setpoint, min(cell.max_setpoint, value))
        if clamped != value:
            log.warning(
                "%s: setpoint %.1f °C outside [%.1f, %.1f], using %.1f °C.",
                cell.name, value, cell.min_setpoint, cell.max_setpoint, clamped,
            )

        clamped = round(clamped, 1)
        link = self._heaters[index]

        ok = link.call(lambda d: d.set_setpoint(clamped) or True)
        if ok:
            self._written_setpoint[index] = clamped
        return bool(ok)

    def _do_set_setpoint(self, index: int, value: float) -> None:
        cell = self.config.cells[index]

        if self._feedback_on[index]:
            self._do_disable_feedback(index)

        if self._write_setpoint(index, value):
            log.info("%s: setpoint set to %.1f °C.", cell.name, value)

    def _do_enable_feedback(self, index: int, target: float) -> None:
        cell = self.config.cells[index]
        fb = self._feedback[index]

        if not target > 0:
            log.error("%s: rate target must be positive.", cell.name)
            return

        if self._feedback_on[index]:
            fb.target = target
            log.info("%s: rate target changed to %.3g Å/s.", cell.name, target)
            return

        setpoint = math.nan
        if self._last is not None:
            setpoint = self._last.cells[index].target_setpoint

        if not math.isfinite(setpoint):
            log.error(
                "%s: cannot start feedback, the current setpoint is unknown.",
                cell.name,
            )
            return

        fb.start(target, setpoint)
        self._feedback_on[index] = True
        self._written_setpoint[index] = setpoint
        log.info(
            "%s: rate feedback ON, target %.3g Å/s, starting from %.1f °C.",
            cell.name, target, setpoint,
        )

    def _do_disable_feedback(self, index: int) -> None:
        if self._feedback_on[index]:
            self._feedback_on[index] = False
            log.info("%s: rate feedback OFF.", self.config.cells[index].name)

    def _run_feedback(self, sample: Sample, dt: float) -> None:
        for index, on in enumerate(self._feedback_on):
            if not on:
                continue

            cell = self.config.cells[index]
            setpoint = self._feedback[index].update(sample.cells[index].rate, dt)

            if setpoint is None:
                continue

            # The controller stores 0.1 °C; avoid redundant writes.
            if abs(round(setpoint, 1) - self._written_setpoint[index]) < 0.05:
                continue

            if not self._write_setpoint(index, setpoint):
                self._feedback_on[index] = False
                log.error(
                    "%s: rate feedback OFF, setpoint could not be written.",
                    cell.name,
                )
