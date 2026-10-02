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
    rate: float = NAN              # Å/s
    thickness: float = NAN         # kÅ
    frequency: float = NAN         # Hz
    rate_target: float = NAN       # Å/s, NaN when feedback is off
    feedback: bool = False


@dataclass(frozen=True)
class Sample:
    timestamp: float               # Unix time
    elapsed: float                 # s since acquisition start
    cells: list[CellSample] = field(default_factory=list)
