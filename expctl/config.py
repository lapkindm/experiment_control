"""
Configuration loading.

The configuration is a TOML file, see ``config.example.toml``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FeedbackConfig:
    """
    Parameters of the rate -> temperature setpoint feedback loop.

    The loop works on the logarithmic rate error ln(target / rate), because
    the evaporation rate depends roughly exponentially on temperature. A
    gain of ``ki = 0.3`` therefore means: a persistent 10 % rate deficit
    raises the setpoint by about 0.03 °C/s, i.e. 1.8 °C/min.
    """

    kp: float = 2.0              # °C per unit of ln(target / rate)
    ki: float = 0.3              # °C per second per unit of ln(target / rate)
    filter_tau: float = 0.0      # s, extra low-pass filter on the rate
    max_slew: float = 10.0       # °C per minute, limit on setpoint changes
    rate_floor: float = 0.02     # fraction of target used as minimum rate


@dataclass
class RateConfig:
    """
    Rate calculation from the QCM thickness (see ``expctl.rate``).
    """

    window: float = 30.0          # s, length of the linear fit
    min_fraction: float = 0.5     # report a rate once the data span this
                                  # fraction of the window
    reset_threshold: float = 0.005  # kÅ, thickness drop treated as a reset


@dataclass
class CellConfig:
    """
    One effusion cell: an Eurotherm controller and a QCM sensor channel.
    """

    name: str
    key: str                     # short identifier used for CSV columns
    sensor: int                  # SQM-160 sensor number (1-6)
    model: str                   # Eurotherm model: "2408" or "3508"
    port: str
    address: int = 1
    baudrate: int | None = None  # None: driver default for the model
    timeout: float = 1.0
    min_setpoint: float = 0.0
    max_setpoint: float = 1000.0
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)


@dataclass
class QCMConfig:
    transport: str = "usb"       # "usb" or "serial"
    port: str | None = None      # serial port, for transport = "serial"
    baudrate: int = 19200
    vid: int | None = None
    pid: int | None = None
    timeout: float = 1.0         # s


@dataclass
class SimulationConfig:
    enabled: bool = False
    speed: float = 1.0           # simulated seconds per real second
    failure_rate: float = 0.0    # probability that a request gets no answer


@dataclass
class Config:
    interval: float = 0.5        # s between acquisitions
    retries: int = 2             # extra attempts for a failed request
    max_missed: int = 5          # consecutive missed requests -> reconnect
    reconnect_delay: float = 5.0 # s between reconnection attempts
    log_directory: Path = Path("logs")
    log_autostart: bool = True
    qcm: QCMConfig = field(default_factory=QCMConfig)
    rate: RateConfig = field(default_factory=RateConfig)
    cells: list[CellConfig] = field(default_factory=list)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)


def _make_key(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_") or "cell"


def _build(cls, data: dict, section: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ValueError(
            f"Unknown option(s) in [{section}]: {', '.join(sorted(unknown))}"
        )
    return cls(**data)


def load_config(path: str | Path) -> Config:
    """
    Load the configuration from a TOML file.
    """

    path = Path(path)

    with path.open("rb") as f:
        data = tomllib.load(f)

    acquisition = data.get("acquisition", {})
    logging_ = data.get("logging", {})

    cells = []
    for raw in data.get("cells", []):
        raw = dict(raw)
        feedback = _build(
            FeedbackConfig, raw.pop("feedback", {}), "cells.feedback"
        )
        raw.setdefault("key", _make_key(raw.get("name", "")))
        cells.append(
            _build(CellConfig, {**raw, "feedback": feedback}, "cells")
        )

    if not cells:
        raise ValueError("The configuration defines no [[cells]].")

    keys = [cell.key for cell in cells]
    if len(set(keys)) != len(keys):
        raise ValueError(f"Cell keys must be unique, got {keys}.")

    if acquisition.get("retries", 2) < 0 or acquisition.get("max_missed", 5) < 1:
        raise ValueError("[acquisition] needs retries >= 0 and max_missed >= 1.")

    for cell in cells:
        if cell.min_setpoint >= cell.max_setpoint:
            raise ValueError(
                f"{cell.name}: min_setpoint must be below max_setpoint."
            )

    log_directory = Path(logging_.get("directory", "logs"))
    if not log_directory.is_absolute():
        log_directory = path.parent / log_directory

    return Config(
        interval=acquisition.get("interval", 0.5),
        retries=acquisition.get("retries", 2),
        max_missed=acquisition.get("max_missed", 5),
        reconnect_delay=acquisition.get("reconnect_delay", 5.0),
        log_directory=log_directory,
        log_autostart=logging_.get("autostart", True),
        qcm=_build(QCMConfig, data.get("qcm", {}), "qcm"),
        rate=_build(RateConfig, data.get("rate", {}), "rate"),
        cells=cells,
        simulation=_build(
            SimulationConfig, data.get("simulation", {}), "simulation"
        ),
    )
