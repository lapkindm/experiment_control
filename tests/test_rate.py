import math

import pytest

from expctl.config import RateConfig
from expctl.rate import ThicknessRate


def test_slope_of_linear_thickness():
    est = ThicknessRate(RateConfig(window=30.0))
    for t in range(60):
        rate = est.update(float(t), 1.0 + 0.0005 * t)   # 0.5 Å/s
    assert rate == pytest.approx(0.5)


def test_nan_until_half_window():
    est = ThicknessRate(RateConfig(window=30.0, min_fraction=0.5))
    rates = [est.update(float(t), 0.001 * t) for t in range(20)]
    assert all(math.isnan(r) for r in rates[:15])
    assert rates[15] == pytest.approx(1.0)


def test_rounding_is_averaged_out():
    # 0.1 Å/s, thickness displayed with 1 Å resolution.
    est = ThicknessRate(RateConfig(window=30.0))
    rates = [
        est.update(float(t), round(0.0001 * t, 3)) for t in range(600)
    ]
    assert all(abs(r - 0.1) < 0.025 for r in rates[30:])


def test_thickness_reset_restarts_fit():
    est = ThicknessRate(RateConfig(window=30.0))
    for t in range(60):
        est.update(float(t), 5.0 + 0.001 * t)
    assert math.isnan(est.update(60.0, 0.0))
    for t in range(61, 80):
        rate = est.update(float(t), 0.001 * (t - 60))
    assert rate == pytest.approx(1.0)


def test_missing_readings_are_skipped():
    est = ThicknessRate(RateConfig(window=30.0))
    for t in range(60):
        thickness = math.nan if t % 3 == 0 else 0.002 * t
        rate = est.update(float(t), thickness)
    assert rate == pytest.approx(2.0)
