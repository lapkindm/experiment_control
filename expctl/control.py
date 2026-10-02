"""
Rate feedback: adjust the cell temperature setpoint to hold a target
deposition rate.

The controller is a PI controller in velocity (incremental) form: each
update computes a *change* of the Eurotherm setpoint. This gives

- bumpless start: it begins from whatever setpoint is active,
- no integral wind-up: the setpoint itself is the integrator state and
  is clamped to the configured limits,
- a natural place to limit the slew rate of the setpoint.

The error is taken in log space, e = ln(target / rate), because the
vapour pressure, and therefore the rate, depends roughly exponentially
on temperature. That keeps the loop gain approximately independent of
the target rate.
"""

from __future__ import annotations

import math

from .config import FeedbackConfig


class RateFeedback:
    def __init__(
        self,
        config: FeedbackConfig,
        min_setpoint: float,
        max_setpoint: float,
    ) -> None:
        self.config = config
        self.min_setpoint = min_setpoint
        self.max_setpoint = max_setpoint

        self.target: float = 0.0
        self.setpoint: float = math.nan
        self.filtered_rate: float = math.nan
        self._previous_error: float | None = None

    def start(self, target: float, setpoint: float) -> None:
        """
        Start regulating towards ``target`` from the current ``setpoint``.
        """

        if not target > 0:
            raise ValueError("Target rate must be positive.")

        self.target = target
        self.setpoint = self._clamp(setpoint)
        self.filtered_rate = math.nan
        self._previous_error = None

    def update(self, rate: float, dt: float) -> float | None:
        """
        Process a new rate measurement taken ``dt`` seconds after the
        previous one.

        Returns the new setpoint, or None if the setpoint should be held
        (invalid measurement).
        """

        if not math.isfinite(rate) or dt <= 0:
            return None

        cfg = self.config

        if math.isfinite(self.filtered_rate):
            alpha = dt / (cfg.filter_tau + dt)
            self.filtered_rate += alpha * (rate - self.filtered_rate)
        else:
            self.filtered_rate = rate

        measured = max(self.filtered_rate, cfg.rate_floor * self.target)
        error = math.log(self.target / measured)

        if self._previous_error is None:
            delta_error = 0.0
        else:
            delta_error = error - self._previous_error
        self._previous_error = error

        delta = cfg.kp * delta_error + cfg.ki * error * dt

        max_delta = cfg.max_slew / 60.0 * dt
        delta = max(-max_delta, min(max_delta, delta))

        self.setpoint = self._clamp(self.setpoint + delta)
        return self.setpoint

    def _clamp(self, value: float) -> float:
        return max(self.min_setpoint, min(self.max_setpoint, value))
