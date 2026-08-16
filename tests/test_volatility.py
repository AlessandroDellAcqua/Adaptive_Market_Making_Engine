import math

import pytest

from lob_sim.agents.volatility import ClockTimeEWMA, EventTimeEWMA


def test_event_time_volatility_is_causal_and_has_floor() -> None:
    estimator = EventTimeEWMA(decay_seconds=1.0, floor_ticks=0.1)
    assert estimator.update(timestamp=0.0, price_ticks=100.0) == 0.1
    first_jump = estimator.update(timestamp=1.0, price_ticks=110.0)
    assert first_jump > 0.1
    with pytest.raises(ValueError):
        estimator.update(timestamp=1.0, price_ticks=111.0)


def test_clock_time_estimator_is_stable_on_constant_prices() -> None:
    estimator = ClockTimeEWMA(interval_seconds=1.0, decay_seconds=2.0, floor_ticks=0.1)
    for timestamp in (0.0, 1.0, 2.0, 3.0):
        assert math.isclose(estimator.update(timestamp=timestamp, price_ticks=100.0), 0.1)

