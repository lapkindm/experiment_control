"""
Acquisition loop.

Each instrument has its own worker thread, which is the only thread that
talks to its port (reads, setpoint writes, open/close). The acquisition
thread keeps the clock: every ``interval`` it asks all workers to read in
parallel and waits for them until shortly before the next tick. A device
that has not answered by then contributes NaN to that sample and does not
delay the others; it is not asked again until it answers, and its late
reading goes into the next sample.

The acquisition thread then computes the rate, runs the rate feedback,
writes the log and notifies listeners. Requests from the GUI (setpoint
changes, feedback on/off) are passed through a queue and executed by the
acquisition thread; setpoint writes are handed to the heater's worker.

Devices that fail repeatedly are closed and reopened after
``reconnect_delay`` (see ``_Link``).
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Callable

from .config import Config
from .control import RateFeedback
from .datalog import CsvLogger
from .devices import Heater, HeaterReading, QCM, QCMChannel
from .rate import ThicknessRate
from .units import RATE_TO_DISPLAY
from .samples import CellSample, Sample

log = logging.getLogger(__name__)

_STOP = object()


class _Link:
    """
    Connection state of one device.

    Instruments occasionally do not answer a request. A failed request is
    retried ``retries`` times; if all attempts fail, the request is
    reported as missed (None) but the device stays open. Only after
    ``max_missed`` consecutive missed requests is the device closed and
    reopened, every ``reconnect_delay`` seconds.

    Used only from the device's worker thread.
    """

    STATS_INTERVAL = 600.0      # s between retry/miss summaries in the log

    def __init__(
        self,
        device,
        reconnect_delay: float,
        retries: int,
        max_missed: int,
    ) -> None:
        self.device = device
        self.reconnect_delay = reconnect_delay
        self.retries = retries
        self.max_missed = max_missed

        self.connected = False
        self.disconnects = 0            # times a connection was lost
        self.missed = 0                 # consecutive missed requests
        self._next_attempt = 0.0

        self._requests = 0
        self._retried = 0
        self._missed_total = 0
        self._stats_since = time.monotonic()

    def call(self, fn: Callable):
        """
        Run ``fn(device)``; return None if the device is unavailable or
        did not respond.
        """

        if not self.connected and not self._connect():
            return None

        self._requests += 1
        self._log_stats()

        error = None
        for attempt in range(1 + self.retries):
            try:
                result = fn(self.device)
            except Exception as exc:
                error = exc
                log.debug(
                    "%s: attempt %d failed: %r", self.device.name, attempt + 1, exc
                )
                continue

            if attempt:
                self._retried += 1
            if self.missed:
                if self.missed > 1:
                    log.info(
                        "%s responds again after %d missed requests.",
                        self.device.name, self.missed,
                    )
                self.missed = 0
            return result

        self.missed += 1
        self._missed_total += 1

        if self.missed >= self.max_missed:
            self._fail(
                f"no valid response to {self.missed} consecutive requests "
                f"({error!r}), reconnecting"
            )
        elif self.missed == 2:
            log.warning(
                "%s: no response (%r), value missing; retrying.",
                self.device.name, error,
            )
        return None

    def close(self) -> None:
        if self.connected:
            try:
                self.device.close()
            except Exception:
                log.exception("Error while closing %s.", self.device.name)
        self.connected = False

    def _connect(self) -> bool:
        if time.monotonic() < self._next_attempt:
            return False
        try:
            self.device.open()
        except Exception as exc:
            self._fail(f"cannot connect: {exc}")
            return False
        self.connected = True
        self.missed = 0
        log.info("%s connected.", self.device.name)
        return True

    def _fail(self, message: str) -> None:
        was_connected = self.connected

        # Report the first failure, not every reconnection attempt.
        if was_connected or self._next_attempt == 0.0:
            log.error("%s: %s", self.device.name, message)

        self.close()
        self.missed = 0
        self._next_attempt = time.monotonic() + self.reconnect_delay

        if was_connected:
            self.disconnects += 1

    def _log_stats(self) -> None:
        now = time.monotonic()
        if now - self._stats_since < self.STATS_INTERVAL:
            return

        if self._retried or self._missed_total:
            log.info(
                "%s: %d requests in the last %.0f min, %d needed a retry, "
                "%d missed.",
                self.device.name, self._requests,
                (now - self._stats_since) / 60, self._retried, self._missed_total,
            )
        self._requests = self._retried = self._missed_total = 0
        self._stats_since = now


class _Worker:
    """
    Thread owning one device; executes its jobs one at a time.
    """

    def __init__(self, link: _Link) -> None:
        self.link = link
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=link.device.name
        )
        # Read in progress, possibly from an earlier tick.
        self.read: Future | None = None

    def submit(self, fn: Callable) -> Future:
        """
        Run ``fn(device)`` with retries on the worker thread. The future
        gives ``(time.monotonic() when finished, result or None)``.
        """

        def job():
            result = self.link.call(fn)
            return time.monotonic(), result

        return self._executor.submit(job)

    def shutdown(self) -> None:
        self._executor.submit(self.link.close)
        self._executor.shutdown(wait=True)


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

        def worker(device):
            return _Worker(
                _Link(
                    device,
                    reconnect_delay=config.reconnect_delay,
                    retries=config.retries,
                    max_missed=config.max_missed,
                )
            )

        self._heaters = [worker(h) for h in heaters]
        self._qcm = worker(qcm)

        self._feedback = [
            RateFeedback(c.feedback, c.min_setpoint, c.max_setpoint)
            for c in config.cells
        ]
        self._rate = [ThicknessRate(config.rate) for _ in config.cells]
        self._feedback_on = [False] * len(config.cells)
        self._written_setpoint = [math.nan] * len(config.cells)
        # Setpoint waiting to be acknowledged by the controller, or NaN.
        self._pending_setpoint = [math.nan] * len(config.cells)
        # Whether the pending setpoint was set by the user, and whether a
        # "not acknowledged" warning was given for it (for logging).
        self._pending_manual = [False] * len(config.cells)
        self._pending_warned = [False] * len(config.cells)
        # Setpoint write in progress: (future, value) or None.
        self._writing: list[tuple[Future, float] | None] = (
            [None] * len(config.cells)
        )
        self._disconnects = [0] * len(config.cells)
        self._t0 = 0.0

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
        t0 = self._t0 = time.monotonic()
        next_time = t0
        previous = None

        log.info("Acquisition started.")

        try:
            while True:
                now = time.monotonic()
                sample = self._acquire(now - t0, deadline=now + 0.9 * interval)

                self._check_heaters()

                if previous is not None:
                    self._run_feedback(sample, (now - previous) * self.time_scale)
                previous = now

                for index in range(len(self.config.cells)):
                    self._send_pending_setpoint(index)

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
            for worker in [*self._heaters, self._qcm]:
                try:
                    worker.shutdown()
                except Exception:
                    log.exception("Stopping %s failed.", worker.link.device.name)
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

    def _acquire(self, elapsed: float, deadline: float) -> Sample:
        """
        Read all devices in parallel; give up on those that have not
        answered by ``deadline``.
        """

        timestamp = time.time()

        workers = [self._qcm, *self._heaters]
        for worker in workers:
            if worker.read is None:
                worker.read = worker.submit(lambda d: d.read())

        wait(
            [worker.read for worker in workers],
            timeout=max(0.0, deadline - time.monotonic()),
        )

        results = []
        for worker in workers:
            if worker.read.done():
                results.append(worker.read.result())
                worker.read = None
            else:
                results.append((math.nan, None))

        (qcm_finished, qcm), *heaters = results
        qcm: dict[int, QCMChannel] = qcm or {}

        # Time of the thickness reading for the rate fit (in simulated
        # time when simulating).
        qcm_time = (qcm_finished - self._t0) * self.time_scale

        cells = []
        for cell, (_, heater), rate in zip(
            self.config.cells, heaters, self._rate
        ):
            heater: HeaterReading | None
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
            values["rate"] = rate.update(qcm_time, thickness)

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

    def _request_setpoint(self, index: int, value: float) -> float:
        """
        Clamp ``value`` to the cell limits and queue it for writing.
        """

        cell = self.config.cells[index]

        clamped = max(cell.min_setpoint, min(cell.max_setpoint, value))
        if clamped != value:
            log.warning(
                "%s: setpoint %.1f °C outside [%.1f, %.1f], using %.1f °C.",
                cell.name, value, cell.min_setpoint, cell.max_setpoint, clamped,
            )

        self._pending_setpoint[index] = round(clamped, 1)
        self._pending_warned[index] = False
        return self._pending_setpoint[index]

    def _send_pending_setpoint(self, index: int) -> None:
        """
        Hand the pending setpoint to the heater's worker, unless a write
        is already in progress.
        """

        value = self._pending_setpoint[index]
        if math.isnan(value) or self._writing[index] is not None:
            return

        future = self._heaters[index].submit(
            lambda d: d.set_setpoint(value) or True
        )
        self._writing[index] = (future, value)

    def _check_heaters(self) -> None:
        """
        Collect finished setpoint writes and handle lost connections.
        """

        for index, worker in enumerate(self._heaters):
            cell = self.config.cells[index]

            writing = self._writing[index]
            if writing is not None and writing[0].done():
                self._writing[index] = None
                future, value = writing
                _, ok = future.result()

                if ok:
                    self._written_setpoint[index] = value
                    if self._pending_setpoint[index] == value:
                        self._pending_setpoint[index] = math.nan
                        if self._pending_manual[index]:
                            log.info(
                                "%s: setpoint set to %.1f °C.", cell.name, value
                            )
                elif (
                    self._pending_manual[index]
                    and not self._pending_warned[index]
                    and self._pending_setpoint[index] == value
                    and worker.link.connected
                ):
                    self._pending_warned[index] = True
                    log.warning(
                        "%s: setpoint %.1f °C not acknowledged yet, retrying.",
                        cell.name, value,
                    )

            # Reading ``disconnects`` from this thread is safe: it is an
            # int only ever incremented by the worker.
            if worker.link.disconnects != self._disconnects[index]:
                self._disconnects[index] = worker.link.disconnects
                self._heater_disconnected(index)

    def _heater_disconnected(self, index: int) -> None:
        cell = self.config.cells[index]

        if self._feedback_on[index]:
            self._feedback_on[index] = False
            log.error("%s: rate feedback OFF, controller disconnected.", cell.name)

        value = self._pending_setpoint[index]
        if not math.isnan(value):
            self._pending_setpoint[index] = math.nan
            log.error(
                "%s: setpoint %.1f °C was not written (controller "
                "disconnected). Set it again after reconnection.",
                cell.name, value,
            )

        self._written_setpoint[index] = math.nan

    def _do_set_setpoint(self, index: int, value: float) -> None:
        if self._feedback_on[index]:
            self._do_disable_feedback(index)

        self._request_setpoint(index, value)
        self._pending_manual[index] = True
        self._send_pending_setpoint(index)

    def _do_enable_feedback(self, index: int, target: float) -> None:
        cell = self.config.cells[index]
        fb = self._feedback[index]

        if not target > 0:
            log.error("%s: rate target must be positive.", cell.name)
            return

        if self._feedback_on[index]:
            fb.target = target
            log.info(
                "%s: rate target changed to %.3g Å/min.",
                cell.name, target * RATE_TO_DISPLAY,
            )
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
        self._pending_setpoint[index] = math.nan
        self._pending_manual[index] = False
        log.info(
            "%s: rate feedback ON, target %.3g Å/min, starting from %.1f °C.",
            cell.name, target * RATE_TO_DISPLAY, setpoint,
        )

    def _do_disable_feedback(self, index: int) -> None:
        if self._feedback_on[index]:
            self._feedback_on[index] = False
            log.info("%s: rate feedback OFF.", self.config.cells[index].name)

    def _run_feedback(self, sample: Sample, dt: float) -> None:
        for index, on in enumerate(self._feedback_on):
            if not on:
                continue

            # Hold while the controller does not answer: the setpoint
            # could not be applied anyway.
            if math.isnan(sample.cells[index].temperature):
                continue

            setpoint = self._feedback[index].update(sample.cells[index].rate, dt)
            if setpoint is None:
                continue

            # The controller stores 0.1 °C; avoid redundant writes.
            if abs(round(setpoint, 1) - self._written_setpoint[index]) < 0.05:
                self._pending_setpoint[index] = math.nan
                continue

            self._request_setpoint(index, setpoint)
            self._pending_manual[index] = False
