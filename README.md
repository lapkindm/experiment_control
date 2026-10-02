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

*Reset thickness & time* (toolbar, asks for confirmation) zeroes the
SQM-160 thickness readings and deposition timer (commands `S` and `T`),
restarts the rate fits and restarts `elapsed_s` in the log; the plots mark
it with a dotted line.

The plots are against the elapsed time, i.e. the time since the last reset
(or the start), in h:mm:ss; data from before a reset stays visible at
negative times. The toolbar shows the current elapsed time. When simulating, these times
are simulated time.

Closing the program leaves the Eurotherms at their current setpoints.

## Threads

Each instrument has its own worker thread; it is the only thread that uses
the instrument's port (reads, setpoint writes, open/close). Every
`interval` the acquisition thread asks all workers to read in parallel
and waits until shortly before the next tick. An instrument that is slow
or retrying does not delay the others or the clock: it contributes empty
values to that sample, is not asked again until it answers, and its late
reading goes into the next sample (the rate fit uses the time of the
reading).

## Unreliable communication

The QCM and the Eurotherms occasionally do not answer a request. Handling
(`expctl/acquisition.py`, `[acquisition]` options):

- a failed request is retried `retries` times (default 2) at once;
- if all attempts fail, that reading is missing (empty in the CSV, "—"
  in the GUI, skipped by the rate fit) but the device stays open;
- after `max_missed` (default 5) consecutive missed requests the device
  is closed and reopened every `reconnect_delay` s;
- setpoint writes are retried every cycle until the controller
  acknowledges them; the rate feedback holds while its controller does
  not answer and is switched off only if the controller is reconnected
  (a pending setpoint is then discarded, with an error in the event log);
- the event log stays quiet for isolated misses; it warns from the second
  consecutive miss and every 10 min lists how many requests needed a
  retry or were missed.

The SQM-160 driver discards stale input before each command, so a reply
that arrives after its timeout cannot be taken for the next answer (the
Eurotherm Modbus driver does this already). Request timeouts are set by
`timeout` in `[qcm]` (default 1 s) and `[[cells]]` (Eurotherm, default
0.3 s, so a retry still fits into one 0.5 s interval).

Each Eurotherm reading (process value, target and working setpoint,
output: Modbus registers 1-5) is a single Modbus request. If a controller
rejects that request when connecting, expctl falls back to reading the
registers one by one and logs a warning.

Test without hardware: `expctl config.toml --simulate --failure-rate 0.2`.

## Logs

`logs/<YYYYmmdd_HHMMSS>.csv` — one row per acquisition (default every 0.5 s):
`time, elapsed_s` (since start or last reset) and for each cell `<key>_T, _SP, _WSP, _output, _rate,
_qcm_rate, _qcm_rate_filtered, _thickness, _frequency, _rate_target,
_feedback`. `_rate` is the fitted rate, `_qcm_rate` the (unfiltered) rate
reported by the SQM-160, `_qcm_rate_filtered` that rate smoothed like the
SQM-160 front panel. A new file starts on
every *Start logging*. Rows are flushed immediately.

`logs/events.log` — setpoint changes, feedback on/off, device errors.

Units (log and GUI): °C, %, Hz, rates in Å/min, thickness in Å. The
target rate is entered in Å/min. The SQM-160 must be in Angstrom display
mode (rate Å/s, thickness Å); expctl logs an error if it is not.

Older logs: before 2026-10-02 (first version) rates were in Å/s and
thickness ×0.001; logs up to commit `315f96c` have thickness ×1000 too
large (wrong kÅ assumption) and an invalid fitted rate.

## Deposition rate

At low rates the rate reported by the QCM is too coarse and noisy. The
rate used for display and feedback is therefore the slope of a
least-squares line through the thickness readings of the last
`[rate] window` seconds (default 30 s; `expctl/rate.py`). It is NaN until
half a window of data exists, it restarts when the thickness drops by
more than `reset_threshold` (crystal change, zeroing on the front panel),
and it lags the true rate by about window/2.

The rate reported by the SQM-160 (`_qcm_rate`) is unfiltered: one
thickness step per time base, i.e. it only takes multiples of about
0.3 Å / 0.3 s ≈ 1 Å/s (60 Å/min). The front panel averages the last
`rate_filter` of these, which equals the thickness change over
`rate_filter × time_base` (2.4 s by default) divided by that time. expctl
computes the same from the thickness (`_qcm_rate_filtered`, settings
read from the SQM-160 on connection) and shows it as "Rate (QCM)" in the
GUI; the CSV has both.

Its noise is set by the thickness resolution: the SQM-160 thickness
moves in steps of its frequency resolution (0.12 Hz, i.e. 0.296 Å at the
film density 0.5 measured on our chamber; finer for denser films) and
flips by about one step. The simulator reproduces this. With a 30 s
window and 0.5 s interval, under rate feedback:

| target rate | noise of the fitted rate | variation of the actual rate | settles within ±5 % |
|---|---|---|---|
| 0.3 Å/min | 56 % | 11 % | no |
| 0.6 Å/min | 25 % | 3 % | no |
| 1.2 Å/min | 13 % | 1 % | ~9 min |
| 3 Å/min | 5 % | 0.2 % | ~5 min |
| 12 Å/min | 1 % | < 0.1 % | ~5 min |
| 60 Å/min | 0.2 % | < 0.1 % | ~5 min |

(The actual rate varies much less than the fitted one because the cell
temperature averages the feedback's corrections.) For very low rates use
a longer window.

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
| `acquisition.py` | one worker thread per instrument (the only thread using its port) + acquisition thread: clock, rate, feedback, logging; GUI requests go through a queue |
| `rate.py` | rate from a linear fit to the thickness |
| `control.py` | rate feedback controller |
| `datalog.py` | CSV logger |
| `gui.py` | PyQt6 / pyqtgraph user interface |

## Tests

```bash
.venv/bin/python -m pytest
```
