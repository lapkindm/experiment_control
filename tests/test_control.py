import math

import pytest

from expctl.config import CellConfig, FeedbackConfig, SimulationConfig
from expctl.control import RateFeedback
from expctl.simulation import SimulatedChamber


def make_feedback(**kwargs):
    return RateFeedback(FeedbackConfig(**kwargs), min_setpoint=20, max_setpoint=1000)


def test_low_rate_raises_setpoint():
    fb = make_feedback()
    fb.start(target=1.0, setpoint=700.0)
    assert fb.update(0.5, dt=1.0) > 700.0


def test_high_rate_lowers_setpoint():
    fb = make_feedback()
    fb.start(target=1.0, setpoint=700.0)
    assert fb.update(2.0, dt=1.0) < 700.0


def test_invalid_rate_holds():
    fb = make_feedback()
    fb.start(target=1.0, setpoint=700.0)
    assert fb.update(math.nan, dt=1.0) is None
    assert fb.setpoint == 700.0


def test_slew_limit():
    fb = make_feedback(max_slew=6.0)       # 0.1 °C/s
    fb.start(target=1.0, setpoint=700.0)
    for _ in range(10):
        fb.update(0.0, dt=1.0)
    assert fb.setpoint == pytest.approx(701.0)


def test_setpoint_clamped():
    fb = RateFeedback(FeedbackConfig(max_slew=1e6), 20.0, 705.0)
    fb.start(target=1.0, setpoint=700.0)
    for _ in range(100):
        fb.update(0.0, dt=1.0)
    assert fb.setpoint == 705.0


def test_rejects_nonpositive_target():
    with pytest.raises(ValueError):
        make_feedback().start(target=0.0, setpoint=700.0)


@pytest.mark.parametrize("target", [0.3, 1.0, 3.0])
def test_closed_loop_with_simulator(target):
    """
    Default gains bring the simulated cell to the target rate and hold it.
    """

    now = [0.0]
    cell = CellConfig(name="A", key="A", sensor=1, model="2408", port="sim")
    chamber = SimulatedChamber(
        [cell], SimulationConfig(), seed=1, clock=lambda: now[0]
    )
    heater, qcm = chamber.heaters[0], chamber.qcm

    # Preheat 30 °C below the 1 Å/s temperature (800 °C).
    heater.set_setpoint(770.0)
    for _ in range(600):
        now[0] += 1.0
        heater.read()

    fb = RateFeedback(cell.feedback, cell.min_setpoint, cell.max_setpoint)
    fb.start(target, heater.read().target_setpoint)

    rates = []
    for step in range(3600):
        now[0] += 1.0
        rate = qcm.read()[1].rate
        setpoint = fb.update(rate, dt=1.0)
        heater.set_setpoint(setpoint)
        if step >= 3000:
            rates.append(chamber.cells[0].true_rate())

    mean = sum(rates) / len(rates)
    assert mean == pytest.approx(target, rel=0.03)
    assert max(rates) < target * 1.05
    assert min(rates) > target * 0.95
