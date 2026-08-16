"""Causal event-time and clock-time EWMA volatility estimators."""

from __future__ import annotations

import math


class EventTimeEWMA:
    """EWMA of log-return variance rate, converted to price volatility."""

    def __init__(self, *, decay_seconds: float = 5.0, floor_ticks: float = 0.01) -> None:
        if decay_seconds <= 0:
            raise ValueError("decay_seconds must be positive")
        if floor_ticks <= 0:
            raise ValueError("floor_ticks must be positive")
        self.decay_seconds = decay_seconds
        self.floor_ticks = floor_ticks
        self._previous_timestamp: float | None = None
        self._previous_price: float | None = None
        self._variance_rate_log = 0.0
        self._sigma_ticks = floor_ticks

    @property
    def sigma_ticks(self) -> float:
        return self._sigma_ticks

    def update(self, *, timestamp: float, price_ticks: float) -> float:
        if price_ticks <= 0:
            raise ValueError("price must be positive")
        if self._previous_timestamp is None:
            self._previous_timestamp = timestamp
            self._previous_price = price_ticks
            return self._sigma_ticks
        assert self._previous_price is not None
        dt = timestamp - self._previous_timestamp
        if dt <= 0:
            raise ValueError("timestamps must increase strictly")
        log_return = math.log(price_ticks / self._previous_price)
        instantaneous_rate = log_return * log_return / dt
        weight = 1.0 - math.exp(-dt / self.decay_seconds)
        self._variance_rate_log = (
            (1.0 - weight) * self._variance_rate_log + weight * instantaneous_rate
        )
        self._sigma_ticks = max(
            price_ticks * math.sqrt(max(self._variance_rate_log, 0.0)),
            self.floor_ticks,
        )
        self._previous_timestamp = timestamp
        self._previous_price = price_ticks
        return self._sigma_ticks


class ClockTimeEWMA:
    """EWMA sampled on a fixed clock grid, with causal carry-forward prices."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        decay_seconds: float = 5.0,
        floor_ticks: float = 0.01,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self._event_estimator = EventTimeEWMA(
            decay_seconds=decay_seconds,
            floor_ticks=floor_ticks,
        )
        self._next_sample_time: float | None = None
        self._last_observation_time: float | None = None
        self._last_observation_price: float | None = None

    @property
    def sigma_ticks(self) -> float:
        return self._event_estimator.sigma_ticks

    def update(self, *, timestamp: float, price_ticks: float) -> float:
        if price_ticks <= 0:
            raise ValueError("price must be positive")
        if self._next_sample_time is None:
            self._next_sample_time = timestamp

        if self._last_observation_time is None:
            self._emit_sample(self._next_sample_time, price_ticks)
            self._next_sample_time += self.interval_seconds
            self._last_observation_time = timestamp
            self._last_observation_price = price_ticks
            return self.sigma_ticks

        assert self._last_observation_price is not None
        if timestamp <= self._last_observation_time:
            raise ValueError("timestamps must increase strictly")
        while self._next_sample_time < timestamp - 1e-12:
            # Carry forward only the last price known before the sample time.
            self._emit_sample(self._next_sample_time, self._last_observation_price)
            self._next_sample_time += self.interval_seconds
        if abs(self._next_sample_time - timestamp) <= 1e-12:
            self._emit_sample(self._next_sample_time, price_ticks)
            self._next_sample_time += self.interval_seconds
        self._last_observation_time = timestamp
        self._last_observation_price = price_ticks
        return self.sigma_ticks

    def _emit_sample(self, timestamp: float, price_ticks: float) -> None:
        self._event_estimator.update(timestamp=timestamp, price_ticks=price_ticks)
