"""
Thin adapters around the hardware drivers.

The rest of the program only talks to the ``Heater`` and ``QCM``
interfaces defined here, so the real instruments and the simulator
(``expctl.simulation``) are interchangeable.

The driver packages are imported lazily, so the simulator works on a
machine without pyusb / minimalmodbus.
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Protocol

from .config import CellConfig, QCMConfig

log = logging.getLogger(__name__)


class HeaterReading(NamedTuple):
    temperature: float           # °C
    target_setpoint: float       # °C
    working_setpoint: float      # °C
    output: float                # %


class QCMChannel(NamedTuple):
    rate: float                  # Å/s in Angstrom display mode, unfiltered
    thickness: float             # Å in Angstrom display mode
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
    def reset(self) -> None: ...


# ----------------------------------------------------------------------
# Eurotherm
# ----------------------------------------------------------------------

class EurothermHeater:
    """
    Eurotherm 2408 / 3508 temperature controller.

    The process value, setpoints and output occupy contiguous Modbus
    registers (1-5), so they are read with one request. If a controller
    rejects that request, the registers are read one by one instead.
    """

    def __init__(self, cell: CellConfig) -> None:
        self.cell = cell
        self.name = f"{cell.name} Eurotherm"
        self._controller = None
        self._block_read = True

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
        self._block_read = self._block_read_supported()

    def _block_read_supported(self, attempts: int = 3) -> bool:
        import minimalmodbus

        for attempt in range(attempts):
            try:
                self._read_block()
                return True
            except Exception as exc:
                if isinstance(exc.__cause__, minimalmodbus.SlaveReportedException):
                    log.warning(
                        "%s rejects reading registers %d-%d at once (%s); "
                        "reading them one by one.",
                        self.name, *self._block_range(), exc,
                    )
                    return False
                if attempt == attempts - 1:
                    self.close()
                    raise

    def close(self) -> None:
        if self._controller is not None:
            try:
                self._controller.close()
            finally:
                self._controller = None

    def read(self) -> HeaterReading:
        if self._block_read:
            return self._read_block()

        c = self._require_open()
        return HeaterReading(
            temperature=c.temperature,
            target_setpoint=c.target_setpoint,
            working_setpoint=c.working_setpoint,
            output=c.output_level,
        )

    @staticmethod
    def _registers():
        from eurotherm.registers.series2000 import (
            OUTPUT_LEVEL, PV, TARGET_SP, WORKING_SP,
        )

        # In HeaterReading order.
        return PV, TARGET_SP, WORKING_SP, OUTPUT_LEVEL

    @classmethod
    def _block_range(cls) -> tuple[int, int]:
        addresses = [r.address for r in cls._registers()]
        return min(addresses), max(addresses)

    def _read_block(self) -> HeaterReading:
        c = self._require_open()
        first, last = self._block_range()
        raw = c.transport.read_registers(first, last - first + 1)

        def value(register):
            v = raw[register.address - first]
            # Two's complement: the output can be negative (heat/cool);
            # temperatures never reach 3276.8 °C.
            if v >= 0x8000:
                v -= 0x10000
            return v / 10 ** register.decimals

        return HeaterReading(*(value(r) for r in self._registers()))

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

    # Front-panel rate smoothing: rate_filter * time_base (s), read from
    # the instrument on connection.
    filter_time: float | None = None

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

        self._check_settings()

    def _check_settings(self) -> None:
        from sqm160 import DisplayMode

        try:
            parameters = self._sqm.system_parameters()
        except Exception as exc:
            log.warning("Could not read the SQM-160 system parameters: %s", exc)
            return

        self.filter_time = parameters.rate_filter * parameters.time_base

        mode = parameters.display_mode
        if mode != DisplayMode.ANGSTROM:
            log.error(
                "SQM-160 display mode is %s, not ANGSTROM: rates and "
                "thicknesses will be in the wrong units.", mode.name,
            )

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

    def reset(self) -> None:
        """
        Zero the thickness readings and the deposition timer.
        """
        if self._sqm is None:
            raise ConnectionError("SQM-160 is not connected.")

        self._sqm.reset_measurement()
        self._sqm.reset_time()
