"""
Deposition rate from the thickness reading.

At low rates the rate reported by the QCM is dominated by noise and its
display resolution. Instead, the rate is taken as the slope of a
least-squares straight line through the thickness readings of the last
``window`` seconds. Using all points (instead of the difference of the
first and last) averages the noise and the quantisation of the thickness
(one step of the crystal frequency resolution, ~0.3 Å).

The estimate lags the true rate by about half the window.
"""

from __future__ import annotations

import math
from collections import deque

from .config import RateConfig


class ThicknessRate:
    def __init__(self, config: RateConfig) -> None:
        self.config = config
        self._points: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self._points.clear()

    def update(self, t: float, thickness: float) -> float:
        """
        Add the thickness (Å) measured at time ``t`` (s) and return the
        rate in Å/s, or NaN while the window holds too little data.
        """

        cfg = self.config

        if math.isfinite(thickness):
            # A drop in thickness means the reading was zeroed (new
            # crystal, thickness reset, film change): start over.
            if self._points and thickness < self._points[-1][1] - cfg.reset_threshold:
                self._points.clear()
            self._points.append((t, thickness))

        while self._points and self._points[0][0] < t - cfg.window:
            self._points.popleft()

        n = len(self._points)
        if n < 3:
            return math.nan

        t_first = self._points[0][0]
        if t - t_first < cfg.min_fraction * cfg.window:
            return math.nan

        mean_t = sum(p[0] - t_first for p in self._points) / n
        mean_d = sum(p[1] for p in self._points) / n

        stt = sum((p[0] - t_first - mean_t) ** 2 for p in self._points)
        if stt == 0:
            return math.nan
        std = sum(
            (p[0] - t_first - mean_t) * (p[1] - mean_d) for p in self._points
        )

        return std / stt


class PanelRate:
    """
    The rate as smoothed by the SQM-160 front panel.

    The panel averages the last ``rate_filter`` raw rates, each the
    thickness change over one ``time_base``. That average equals the
    thickness change over ``rate_filter * time_base`` divided by that
    time, which is computed here from the (less frequent) readings
    available to us.
    """

    def __init__(self, reset_threshold: float) -> None:
        self.reset_threshold = reset_threshold
        self._points: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self._points.clear()

    def update(self, t: float, thickness: float, span: float) -> float:
        """
        Add the thickness (Å) at time ``t`` (s); return the rate (Å/s)
        over the last ``span`` seconds, or NaN without enough data.
        """

        if not math.isfinite(thickness):
            return math.nan

        if self._points and thickness < self._points[-1][1] - self.reset_threshold:
            self._points.clear()
        self._points.append((t, thickness))

        # Keep one point at or before t - span.
        while len(self._points) > 1 and self._points[1][0] <= t - span:
            self._points.popleft()

        t_old, d_old = self._points[0]
        if t_old > t - 0.75 * span:
            return math.nan
        return (thickness - d_old) / (t - t_old)
