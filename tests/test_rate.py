import math

import pytest

from expctl.config import RateConfig
from expctl.rate import ThicknessRate


def test_slope_of_linear_thickness():
    est = ThicknessRate(RateConfig(window=30.0))
    for t in range(60):
        rate = est.update(float(t), 1000.0 + 0.5 * t)   # 0.5 Å/s
    assert rate == pytest.approx(0.5)


def test_nan_until_half_window():
    est = ThicknessRate(RateConfig(window=30.0, min_fraction=0.5))
    rates = [est.update(float(t), 1.0 * t) for t in range(20)]
    assert all(math.isnan(r) for r in rates[:15])
    assert rates[15] == pytest.approx(1.0)


def test_quantisation_is_averaged_out():
    # 0.05 Å/s (3 Å/min), thickness in 0.296 Å steps, read every 0.5 s.
    step = 0.296
    est = ThicknessRate(RateConfig(window=30.0))
    rates = [
        est.update(0.5 * i, round(0.05 * 0.5 * i / step) * step)
        for i in range(1200)
    ]
    assert all(abs(r - 0.05) < 0.005 for r in rates[60:])


def test_thickness_reset_restarts_fit():
    est = ThicknessRate(RateConfig(window=30.0))
    for t in range(60):
        est.update(float(t), 500.0 + 1.0 * t)
    assert math.isnan(est.update(60.0, 0.0))
    for t in range(61, 80):
        rate = est.update(float(t), 1.0 * (t - 60))
    assert rate == pytest.approx(1.0)


def test_noise_does_not_restart_fit():
    # Readings flipping by one 0.296 Å step (no deposition) must not be
    # taken for a thickness reset.
    est = ThicknessRate(RateConfig(window=30.0))
    for i in range(200):
        rate = est.update(0.5 * i, -0.296 * (i % 2))
    assert abs(rate) < 0.01


def test_missing_readings_are_skipped():
    est = ThicknessRate(RateConfig(window=30.0))
    for t in range(60):
        thickness = math.nan if t % 3 == 0 else 2.0 * t
        rate = est.update(float(t), thickness)
    assert rate == pytest.approx(2.0)
