"""
Units.

The SQM-160 (Angstrom display mode) reports rates in Å/s and thickness
in Å; these are used internally. The GUI and the CSV log show rates in
Å/min.
"""

RATE_TO_DISPLAY = 60.0          # Å/s -> Å/min

# Scale factors from internal to display units, by CellSample field.
DISPLAY_SCALE = {
    "rate": RATE_TO_DISPLAY,
    "rate_target": RATE_TO_DISPLAY,
    "qcm_rate": RATE_TO_DISPLAY,
    "qcm_rate_filtered": RATE_TO_DISPLAY,
}
