import csv
import math

from expctl.datalog import CsvLogger
from expctl.samples import CellSample, Sample


def test_csv_units(tmp_path):
    logger = CsvLogger(tmp_path, ["A"])
    path = logger.start()
    logger.write(
        Sample(
            timestamp=0.0,
            elapsed=1.0,
            cells=[
                CellSample(
                    temperature=725.7,
                    rate=0.2,               # Å/s
                    qcm_rate=0.16,          # Å/s
                    rate_target=0.05,       # Å/s
                    thickness=0.123,        # kÅ
                    frequency=5999994.123,  # Hz
                    feedback=True,
                )
            ],
        )
    )
    logger.stop()

    with path.open() as f:
        row = next(csv.DictReader(f))

    assert row["A_T"] == "725.7"
    assert row["A_rate"] == "12"            # Å/min
    assert row["A_qcm_rate"] == "9.6"
    assert row["A_rate_target"] == "3"
    assert row["A_thickness"] == "123"      # Å
    assert row["A_frequency"] == "5999994.123"
    assert row["A_feedback"] == "1"
    assert row["A_SP"] == ""                # NaN
