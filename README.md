# expctl — effusion cell deposition control

Logs and displays the temperatures of two effusion cells (Eurotherm 2408 /
3508, Modbus RTU) and the deposition rates measured by an INFICON SQM-160
(one sensor channel per cell), and can regulate each cell's temperature
setpoint to hold a target deposition rate.

Built on the drivers
[sqm160](https://github.com/lapkindm/sqm160) and
[eurotherm2000](https://github.com/lapkindm/eurotherm2000).

## Installation

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
```

This also installs both drivers from GitHub.

## Usage

```bash
cp config.example.toml config.toml   # then edit ports, sensors, limits
expctl config.toml                   # GUI, real hardware
expctl config.toml --simulate        # GUI, simulated chamber
expctl config.toml --speed 30        # simulated, 30x faster than real time
expctl config.toml --no-gui          # log only, status lines on stdout
```

The GUI shows, per cell, temperature, setpoints, output, rate, thickness
and crystal frequency, plus live plots of temperatures and rates. Setpoints
can be written from the GUI; every write (manual or by the feedback) is
clamped to `min_setpoint`/`max_setpoint` of that cell.

Closing the program leaves the Eurotherms at their current setpoints.

## Logs

`logs/<YYYYmmdd_HHMMSS>.csv` — one row per acquisition (default every 1 s):
`time, elapsed_s` and for each cell `<key>_T, _SP, _WSP, _output, _rate,
_thickness, _frequency, _rate_target, _feedback`. A new file starts on
every *Start logging*. Rows are flushed immediately.

`logs/events.log` — setpoint changes, feedback on/off, device errors.

Units are those of the instruments: °C, %, and for the SQM-160 in Angstrom
display mode Å/s and kÅ.

## Rate feedback

Per cell, *Rate feedback: ON* starts a PI loop that adjusts the Eurotherm
target setpoint so the QCM rate approaches the target rate. Details in
`expctl/control.py`:

- velocity-form PI: starts from the current setpoint, no integral wind-up;
- error `ln(target / rate)`, so the gain is roughly independent of the rate
  (the rate depends ~exponentially on temperature);
- the rate is low-pass filtered (`filter_tau`), setpoint changes are
  limited to `max_slew` °C/min and clamped to the cell limits;
- invalid rate readings hold the setpoint; a failed setpoint write, or a
  manual setpoint change, switches the feedback off.

The default gains are tuned on the simulator only (thermal time constant
40 s, Ea = 2 eV). Retune `kp`, `ki` on the real cells, starting with small
`max_slew`.

## Architecture

| Module | Role |
|---|---|
| `config.py` | TOML configuration |
| `devices.py` | adapters around the drivers (`Heater`, `QCM` interfaces) |
| `simulation.py` | simulated chamber implementing the same interfaces |
| `acquisition.py` | background thread: owns all devices, polls, runs feedback, reconnects after errors; GUI requests go through a queue |
| `control.py` | rate feedback controller |
| `datalog.py` | CSV logger |
| `gui.py` | PyQt6 / pyqtgraph user interface |

## Tests

```bash
.venv/bin/python -m pytest
```
