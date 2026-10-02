"""
Data records produced by the acquisition loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

NAN = math.nan


@dataclass(frozen=True)
class CellSample:
    temperature: float = NAN       # °C
    target_setpoint: float = NAN   # °C
    working_setpoint: float = NAN  # °C
    output: float = NAN            # %
    rate: float = NAN              # Å/s, fitted from the thickness
    qcm_rate: float = NAN          # Å/s, as reported by the QCM (unfiltered)
    qcm_rate_filtered: float = NAN # Å/s, smoothed like the QCM front panel
    thickness: float = NAN         # Å
    frequency: float = NAN         # Hz
    rate_target: float = NAN       # Å/s, NaN when feedback is off
    feedback: bool = False


@dataclass(frozen=True)
class Sample:
    timestamp: float               # Unix time
    elapsed: float                 # s since acquisition start or last reset
    cells: list[CellSample] = field(default_factory=list)
    reset: bool = False            # first sample after a thickness/time reset
    since_start: float = 0.0       # s since acquisition start, continuous

    # elapsed and since_start are in simulated time when simulating.
