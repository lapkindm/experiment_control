"""
CSV data logging.

Each logging session writes one CSV file, ``<directory>/<timestamp>.csv``.
Rows are flushed immediately, so the file is complete up to the last
sample even if the program crashes.

Units: °C, %, Hz, rates in Å/min, thickness in Å (see ``expctl.units``).
"""

from __future__ import annotations

import csv
import math
import threading
from datetime import datetime
from pathlib import Path

from .samples import Sample
from .units import DISPLAY_SCALE

CELL_FIELDS = [
    ("T", "temperature"),
    ("SP", "target_setpoint"),
    ("WSP", "working_setpoint"),
    ("output", "output"),
    ("rate", "rate"),
    ("qcm_rate", "qcm_rate"),
    ("thickness", "thickness"),
    ("frequency", "frequency"),
    ("rate_target", "rate_target"),
    ("feedback", "feedback"),
]


def columns(cell_keys: list[str]) -> list[str]:
    cols = ["time", "elapsed_s"]
    for key in cell_keys:
        cols += [f"{key}_{name}" for name, _ in CELL_FIELDS]
    return cols


def _format(value, scale: float = 1.0) -> str:
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float):
        # 12 significant digits drop float noise from the unit scaling
        # and keep the full crystal frequency resolution.
        # (+ 0.0 turns -0.0 into 0.0.)
        return "" if math.isnan(value) else f"{value * scale + 0.0:.12g}"
    return str(value)


class CsvLogger:
    """
    Thread-safe CSV logger; ``write`` is a no-op while not logging.
    """

    def __init__(self, directory: Path, cell_keys: list[str]) -> None:
        self.directory = Path(directory)
        self.cell_keys = cell_keys
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self.path: Path | None = None

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self) -> Path:
        with self._lock:
            self._close()

            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.directory / f"{stamp}.csv"

            self._file = path.open("w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(columns(self.cell_keys))
            self._file.flush()
            self.path = path
            return path

    def stop(self) -> None:
        with self._lock:
            self._close()

    def write(self, sample: Sample) -> None:
        with self._lock:
            if self._writer is None:
                return

            row = [
                datetime.fromtimestamp(sample.timestamp).isoformat(
                    timespec="milliseconds"
                ),
                f"{sample.elapsed:.3f}",
            ]
            for cell in sample.cells:
                row += [
                    _format(getattr(cell, attr), DISPLAY_SCALE.get(attr, 1.0))
                    for _, attr in CELL_FIELDS
                ]

            self._writer.writerow(row)
            self._file.flush()

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None
