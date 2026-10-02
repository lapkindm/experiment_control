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

The GUI shows, per cell, temperature, setpoints, output, deposition rate
(fitted, see below), the rate reported by the QCM, thickness and crystal
frequency, plus live plots of temperatures and rates. Setpoints
can be written from the GUI; every write (manual or by the feedback) is
clamped to `min_setpoint`/`max_setpoint` of that cell.

Closing the program leaves the Eurotherms at their current setpoints.

## Logs

`logs/<YYYYmmdd_HHMMSS>.csv` — one row per acquisition (default every 1 s):
`time, elapsed_s` and for each cell `<key>_T, _SP, _WSP, _output, _rate,
_qcm_rate, _thickness, _frequency, _rate_target, _feedback`. `_rate` is the
fitted rate, `_qcm_rate` the rate reported by the SQM-160. A new file starts on
every *Start logging*. Rows are flushed immediately.

`logs/events.log` — setpoint changes, feedback on/off, device errors.

Units are those of the instruments: °C, %, and for the SQM-160 in Angstrom
display mode Å/s and kÅ.

## Deposition rate

At low rates the rate reported by the QCM is too coarse and noisy. The
rate used for display and feedback is therefore the slope of a
least-squares line through the thickness readings of the last
`[rate] window` seconds (default 30 s; `expctl/rate.py`). It is NaN until
half a window of data exists, it restarts when the thickness drops
(crystal change, thickness reset), and it lags the true rate by about
window/2. This assumes the SQM-160 is in Angstrom display mode
(thickness in kÅ).

Its noise is set by the thickness resolution (1 Å) and noise: in the
simulator (0.3 Å thickness noise) it is about 6 % rms at 0.02 Å/s,
2 % at 0.05 Å/s and below 1 % from 0.2 Å/s, with a 30 s window. For very
low rates use a longer window.

## Rate feedback

Per cell, *Rate feedback: ON* starts a PI loop that adjusts the Eurotherm
target setpoint so the QCM rate approaches the target rate. Details in
`expctl/control.py`:

- velocity-form PI: starts from the current setpoint, no integral wind-up;
- error `ln(target / rate)`, so the gain is roughly independent of the rate
  (the rate depends ~exponentially on temperature);
- input is the fitted rate (optionally filtered further, `filter_tau`);
  setpoint changes are limited to `max_slew` °C/min and clamped to the
  cell limits;
- invalid rate readings hold the setpoint; a failed setpoint write, or a
  manual setpoint change, switches the feedback off.

The default gains (kp = 2, ki = 0.3) are tuned on the simulator only
(thermal time constant 40 s, Ea = 2 eV, 30 s rate fit): from 20 °C below,
they settle within ±5 % in 5–9 min for 0.05–1 Å/s, overshoot ≤ 4 %. Retune `kp`, `ki` on the real cells, starting with small
`max_slew`.

## Architecture

| Module | Role |
|---|---|
| `config.py` | TOML configuration |
| `devices.py` | adapters around the drivers (`Heater`, `QCM` interfaces) |
| `simulation.py` | simulated chamber implementing the same interfaces |
| `acquisition.py` | background thread: owns all devices, polls, runs feedback, reconnects after errors; GUI requests go through a queue |
| `rate.py` | rate from a linear fit to the thickness |
| `control.py` | rate feedback controller |
| `datalog.py` | CSV logger |
| `gui.py` | PyQt6 / pyqtgraph user interface |

## Tests

```bash
.venv/bin/python -m pytest
```
