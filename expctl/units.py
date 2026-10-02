"""
Units.

Internally the SQM-160 units are kept (Angstrom display mode): rate in
Å/s, thickness in kÅ. The GUI and the CSV log use Å/min and Å.
"""

RATE_TO_DISPLAY = 60.0          # Å/s -> Å/min
THICKNESS_TO_DISPLAY = 1000.0   # kÅ -> Å

# Scale factors from internal to display units, by CellSample field.
DISPLAY_SCALE = {
    "rate": RATE_TO_DISPLAY,
    "rate_target": RATE_TO_DISPLAY,
    "qcm_rate": RATE_TO_DISPLAY,
    "thickness": THICKNESS_TO_DISPLAY,
}
