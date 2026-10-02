"""
EurothermHeater against a fake Modbus RTU controller on a pseudo-terminal.
"""

import logging
import os
import struct
import sys
import threading

import pytest

pytest.importorskip("eurotherm")
if sys.platform == "win32":
    pytest.skip("needs a pseudo-terminal", allow_module_level=True)

import tty

from expctl.config import CellConfig
from expctl.devices import EurothermHeater

# Eurotherm Series 2000: 1 PV, 2 target SP, 3 output, 4 deviation, 5 working SP
REGISTERS = {1: 7253, 2: 7260, 3: (-125) & 0xFFFF, 4: 0xFFF9, 5: 7258}


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


class FakeEurotherm:
    def __init__(self, illegal=()):
        self.registers = dict(REGISTERS)
        self.illegal = set(illegal)
        self.requests = 0
        self.master, slave = os.openpty()
        tty.setraw(self.master)
        tty.setraw(slave)
        self.port = os.ttyname(slave)
        self._slave = slave
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        buf = b""
        while True:
            try:
                buf += os.read(self.master, 256)
            except OSError:
                return
            while len(buf) >= 8:
                n = 8 if buf[1] in (3, 6) else 9 + buf[6]
                if len(buf) < n:
                    break
                frame, buf = buf[:n], buf[n:]
                self.requests += 1
                os.write(self.master, self._answer(frame))

    def _answer(self, frame):
        address, function, register, value = struct.unpack(">BBHH", frame[:6])
        if function == 3:
            wanted = range(register, register + value)
            if self.illegal & set(wanted):
                body = bytes([address, 0x83, 2])
            else:
                body = bytes([address, 3, 2 * value]) + b"".join(
                    struct.pack(">H", self.registers.get(r, 0)) for r in wanted
                )
        else:  # 6 or 16: write
            if function == 16:
                value = struct.unpack(">H", frame[7:9])[0]
            self.registers[register] = value
            body = frame[:6]
        return body + crc16(body)

    def close(self):
        os.close(self.master)
        os.close(self._slave)


@pytest.fixture
def heater_for():
    created = []

    def make(fake, model="2408"):
        heater = EurothermHeater(
            CellConfig(name="A", key="A", sensor=1, model=model, port=fake.port)
        )
        created.append((heater, fake))
        return heater

    yield make

    for heater, fake in created:
        heater.close()
        fake.close()


@pytest.mark.parametrize("model", ["2408", "3508"])
def test_block_read_is_one_request(heater_for, model):
    fake = FakeEurotherm()
    heater = heater_for(fake, model)
    heater.open()

    before = fake.requests
    reading = heater.read()

    assert fake.requests - before == 1
    assert reading.temperature == pytest.approx(725.3)
    assert reading.target_setpoint == pytest.approx(726.0)
    assert reading.working_setpoint == pytest.approx(725.8)
    assert reading.output == pytest.approx(-12.5)


def test_falls_back_to_single_reads(heater_for, caplog):
    fake = FakeEurotherm(illegal={4})
    heater = heater_for(fake)

    with caplog.at_level(logging.WARNING):
        heater.open()
    assert "one by one" in caplog.text

    before = fake.requests
    reading = heater.read()

    assert fake.requests - before == 4
    assert reading.temperature == pytest.approx(725.3)
    assert reading.working_setpoint == pytest.approx(725.8)


def test_set_setpoint(heater_for):
    fake = FakeEurotherm()
    heater = heater_for(fake)
    heater.open()

    heater.set_setpoint(701.2)
    assert heater.read().target_setpoint == pytest.approx(701.2)
