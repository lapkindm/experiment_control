"""
Thin adapters around the hardware drivers.

The rest of the program only talks to the ``Heater`` and ``QCM``
interfaces defined here, so the real instruments and the simulator
(``expctl.simulation``) are interchangeable.

The driver packages are imported lazily, so the simulator works on a
machine without pyusb / minimalmodbus.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol

from .config import CellConfig, QCMConfig


class HeaterReading(NamedTuple):
    temperature: float           # °C
    target_setpoint: float       # °C
    working_setpoint: float      # °C
    output: float                # %


class QCMChannel(NamedTuple):
    rate: float                  # Å/s in Angstrom display mode
    thickness: float             # kÅ in Angstrom display mode
    frequency: float             # Hz


class Heater(Protocol):
    name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def read(self) -> HeaterReading: ...
    def set_setpoint(self, value: float) -> None: ...


class QCM(Protocol):
    name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def read(self) -> dict[int, QCMChannel]: ...


# ----------------------------------------------------------------------
# Eurotherm
# ----------------------------------------------------------------------

class EurothermHeater:
    """
    Eurotherm 2408 / 3508 temperature controller.
    """

    def __init__(self, cell: CellConfig) -> None:
        self.cell = cell
        self.name = f"{cell.name} Eurotherm"
        self._controller = None

    def open(self) -> None:
        from eurotherm import Eurotherm2408, Eurotherm3508

        classes = {"2408": Eurotherm2408, "3508": Eurotherm3508}

        try:
            cls = classes[str(self.cell.model)]
        except KeyError:
            raise ValueError(
                f"Unsupported Eurotherm model {self.cell.model!r}, "
                f"expected one of {sorted(classes)}."
            ) from None

        # We are the only user of the port: keep it open instead of
        # reopening it for every Modbus transaction.
        kwargs = dict(
            port=self.cell.port,
            address=self.cell.address,
            timeout=self.cell.timeout,
            keep_open=True,
        )
        if self.cell.baudrate is not None:
            kwargs["baudrate"] = self.cell.baudrate

        controller = cls(**kwargs)

        if not controller.ping():
            controller.close()
            raise ConnectionError(
                f"{self.name} does not respond on {self.cell.port}."
            )

        self._controller = controller

    def close(self) -> None:
        if self._controller is not None:
            try:
                self._controller.close()
            finally:
                self._controller = None

    def read(self) -> HeaterReading:
        c = self._require_open()
        return HeaterReading(
            temperature=c.temperature,
            target_setpoint=c.target_setpoint,
            working_setpoint=c.working_setpoint,
            output=c.output_level,
        )

    def set_setpoint(self, value: float) -> None:
        self._require_open().target_setpoint = value

    def _require_open(self):
        if self._controller is None:
            raise ConnectionError(f"{self.name} is not connected.")
        return self._controller


# ----------------------------------------------------------------------
# SQM-160
# ----------------------------------------------------------------------

class SQM160Monitor:
    """
    INFICON SQM-160 deposition monitor (all sensors read with one command).
    """

    name = "SQM-160"

    def __init__(self, config: QCMConfig) -> None:
        self.config = config
        self._sqm = None

    def open(self) -> None:
        from sqm160 import SQM160, SerialTransport

        cfg = self.config

        if cfg.transport == "usb":
            sqm = SQM160(
                vid=cfg.vid,
                pid=cfg.pid,
                timeout=int(cfg.timeout * 1000),
            )
        elif cfg.transport == "serial":
            if not cfg.port:
                raise ValueError("[qcm] port is required for serial transport.")
            sqm = SQM160(
                transport=SerialTransport(
                    cfg.port,
                    baudrate=cfg.baudrate,
                    timeout=cfg.timeout,
                )
            )
        else:
            raise ValueError(
                f"Unknown QCM transport {cfg.transport!r}, "
                "expected 'usb' or 'serial'."
            )

        sqm.open()
        self._sqm = sqm

    def close(self) -> None:
        if self._sqm is not None:
            try:
                self._sqm.close()
            finally:
                self._sqm = None

    def read(self) -> dict[int, QCMChannel]:
        if self._sqm is None:
            raise ConnectionError("SQM-160 is not connected.")

        return {
            sensor: QCMChannel(m.rate, m.thickness, m.frequency)
            for sensor, m in self._sqm.sensor_measurements().items()
        }
