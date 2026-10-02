"""
Print what the SQM-160 reports, to check units and noise.

Read-only: only query commands are sent, no settings are changed.
Close expctl before running (the QCM can have only one user).

    .venv/bin/python tools/qcm_diagnose.py config.toml [seconds]
"""

from __future__ import annotations

import statistics
import sys
import time

from expctl.config import load_config
from expctl.devices import SQM160Monitor


def main() -> None:
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    sensors = [cell.sensor for cell in config.cells]

    monitor = SQM160Monitor(config.qcm)
    monitor.open()
    sqm = monitor._sqm

    try:
        print("Firmware:          ", sqm.firmware_version())
        print("Channels:          ", sqm.channel_count())
        print("System 1 (B?):     ", sqm.query_string("B?"))
        print("  parsed:          ", sqm.system_parameters())
        print("System 2 (C?):     ", sqm.query_string("C?"))
        print()
        print("Raw W response:    ", repr(sqm.query_string("W")))
        for s in sensors:
            print(
                f"Sensor {s}: L{s} (rate) = {sqm.query_string(f'L{s}')!r}, "
                f"N{s} (thickness) = {sqm.query_string(f'N{s}')!r}, "
                f"P{s} (frequency) = {sqm.query_string(f'P{s}')!r}"
            )
        print("Average M (rate) = ", repr(sqm.query_string("M")),
              " O (thickness) = ", repr(sqm.query_string("O")))
        print()

        print(f"Reading W every 0.5 s for {seconds:g} s "
              "(compare with the front panel):")
        print("   t  " + "".join(
            f"| sensor {s}: rate      thickness       frequency   " for s in sensors
        ))

        values = {s: {"rate": [], "thickness": []} for s in sensors}
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            readings = sqm.sensor_measurements()
            line = f"{time.monotonic() - t0:5.1f} "
            for s in sensors:
                m = readings.get(s)
                if m is None:
                    line += f"| sensor {s}: missing" + " " * 33
                    continue
                values[s]["rate"].append(m.rate)
                values[s]["thickness"].append(m.thickness)
                line += (
                    f"| {m.rate:14.4f} {m.thickness:14.4f} "
                    f"{m.frequency:14.3f}  "
                )
            print(line)
            time.sleep(0.5)

        print()
        for s in sensors:
            for name, series in values[s].items():
                if len(series) > 1:
                    print(
                        f"Sensor {s} {name:9s}: min {min(series):.4f}  "
                        f"max {max(series):.4f}  "
                        f"std {statistics.stdev(series):.4f}  "
                        f"distinct values {len(set(series))}"
                    )
    finally:
        monitor.close()


if __name__ == "__main__":
    main()
