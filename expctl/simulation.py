"""
Simulated chamber for development without hardware.

Each simulated cell has
- a temperature following the setpoint with first-order lag
  (stand-in for the Eurotherm PID loop and the thermal mass of the cell),
- an Arrhenius-like deposition rate  r = A * exp(-Ea / kB T),
  low-pass filtered (QCM rate filter) and with multiplicative noise.

The simulation advances on demand from the wall clock, multiplied by
``speed``, so it can run faster than real time for testing.
"""

from __future__ import annotations

import math
import random
import time
from typing import Callable

from .config import CellConfig, SimulationConfig
from .devices import HeaterReading, QCMChannel

KB = 8.617e-5          # eV/K


class _SimCell:
    def __init__(
        self,
        cell: CellConfig,
        reference_temperature: float,
        activation_energy: float = 2.0,
        thermal_tau: float = 40.0,
        qcm_tau: float = 3.0,
        noise: float = 0.02,
    ) -> None:
        self.cell = cell
        self.temperature = 25.0
        self.setpoint = 25.0
        self.output = 0.0
        self.rate = 0.0
        self.filtered_rate = 0.0
        self.thickness = 0.0           # kÅ
        self.frequency = 6.0e6         # Hz

        self.activation_energy = activation_energy
        self.thermal_tau = thermal_tau
        self.qcm_tau = qcm_tau
        self.noise = noise

        # Prefactor chosen so that the rate is 1 Å/s at the reference T.
        self.prefactor = math.exp(
            activation_energy / (KB * (reference_temperature + 273.15))
        )

    def true_rate(self) -> float:
        t = self.temperature + 273.15
        return self.prefactor * math.exp(-self.activation_energy / (KB * t))

    def advance(self, dt: float) -> None:
        if dt <= 0:
            return

        error = self.setpoint - self.temperature
        self.temperature += error * (1 - math.exp(-dt / self.thermal_tau))
        self.output = max(0.0, min(100.0, 20.0 + 2.0 * error
                                   + self.temperature / 20.0))

        rate = self.true_rate()
        alpha = 1 - math.exp(-dt / self.qcm_tau)
        self.filtered_rate += (rate - self.filtered_rate) * alpha

        self.thickness += rate * dt / 1000.0
        self.frequency -= rate * dt * 0.05


class SimulatedChamber:
    """
    Provides simulated heaters and a simulated SQM-160 sharing one state.
    """

    def __init__(
        self,
        cells: list[CellConfig],
        config: SimulationConfig,
        seed: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.speed = config.speed
        self._random = random.Random(seed)
        self._clock = clock
        self._last = clock()

        # Reference temperatures (1 Å/s) for the simulated materials.
        references = [800.0, 500.0, 650.0, 950.0, 400.0, 1100.0]

        self.cells = [
            _SimCell(cell, references[i % len(references)])
            for i, cell in enumerate(cells)
        ]

        self.heaters = [SimulatedHeater(self, i) for i in range(len(cells))]
        self.qcm = SimulatedQCM(self)

    def update(self) -> None:
        now = self._clock()
        dt = (now - self._last) * self.speed
        self._last = now

        for cell in self.cells:
            cell.advance(dt)

    def measured_rate(self, cell: _SimCell) -> float:
        return cell.filtered_rate * (1 + self._random.gauss(0, cell.noise))


class SimulatedHeater:
    def __init__(self, chamber: SimulatedChamber, index: int) -> None:
        self._chamber = chamber
        self._cell = chamber.cells[index]
        self.name = f"{self._cell.cell.name} Eurotherm (simulated)"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self) -> HeaterReading:
        self._chamber.update()
        c = self._cell
        return HeaterReading(
            temperature=round(c.temperature, 1),
            target_setpoint=round(c.setpoint, 1),
            working_setpoint=round(c.setpoint, 1),
            output=round(c.output, 1),
        )

    def set_setpoint(self, value: float) -> None:
        self._chamber.update()
        # The real controller stores one decimal.
        self._cell.setpoint = round(value, 1)


class SimulatedQCM:
    name = "SQM-160 (simulated)"

    def __init__(self, chamber: SimulatedChamber) -> None:
        self._chamber = chamber

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self) -> dict[int, QCMChannel]:
        self._chamber.update()
        return {
            c.cell.sensor: QCMChannel(
                rate=round(self._chamber.measured_rate(c), 2),
                thickness=round(c.thickness, 3),
                frequency=round(c.frequency, 3),
            )
            for c in self._chamber.cells
        }
