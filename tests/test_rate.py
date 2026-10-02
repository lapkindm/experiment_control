import math
import statistics

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


from expctl.rate import PanelRate


def test_panel_rate_of_linear_thickness():
    panel = PanelRate(reset_threshold=5.0)
    for i in range(20):
        rate = panel.update(0.5 * i, 100.0 + 0.2 * 0.5 * i, span=2.4)
    assert rate == pytest.approx(0.2)


def test_panel_rate_nan_until_enough_data():
    panel = PanelRate(reset_threshold=5.0)
    rates = [panel.update(0.5 * i, 0.1 * i, span=2.4) for i in range(6)]
    assert all(math.isnan(r) for r in rates[:4])   # less than 1.8 s
    assert rates[4] == pytest.approx(0.2)


def test_panel_rate_noise_like_front_panel():
    # No deposition, thickness flipping by one 0.296 Å step, read every
    # 0.5 s: the smoothed rate stays within about ±0.1 Å/s (front panel),
    # while the raw rate jumps by ±0.99 Å/s.
    import random

    rng = random.Random(0)
    panel = PanelRate(reset_threshold=5.0)
    rates = [
        panel.update(0.5 * i, 0.296 * rng.choice([-1, 0, 0, 1]), span=2.4)
        for i in range(400)
    ]
    rates = [r for r in rates if r == r]
    assert max(abs(r) for r in rates) < 0.3
    assert statistics.pstdev(rates) < 0.15     # raw rate: ~0.65


def test_panel_rate_restarts_on_thickness_drop():
    panel = PanelRate(reset_threshold=5.0)
    for i in range(10):
        panel.update(0.5 * i, 500.0 + i, span=2.4)
    assert math.isnan(panel.update(5.0, 0.0, span=2.4))
