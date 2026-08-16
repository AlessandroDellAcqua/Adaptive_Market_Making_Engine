"""Seeded synthetic market-path generator.

The generator is deliberately exogenous. This gives strategy comparisons a
common price path and makes it possible to isolate execution and inventory
effects before adding market impact.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random


@dataclass(frozen=True, slots=True)
class PathConfig:
    initial_price_ticks: int = 10_000
    dt_seconds: float = 0.1
    horizon_seconds: float = 20.0
    volatility_ticks: float = 5.0
    drift_ticks_per_second: float = 0.0
    half_spread_ticks: int = 2
    depth: int = 10
    flow_bias: float = 0.0

    def __post_init__(self) -> None:
        if self.initial_price_ticks <= 0:
            raise ValueError("initial price must be positive")
        if self.dt_seconds <= 0 or self.horizon_seconds <= 0:
            raise ValueError("time parameters must be positive")
        if self.volatility_ticks < 0:
            raise ValueError("volatility must be non-negative")
        if self.half_spread_ticks <= 0 or self.depth <= 0:
            raise ValueError("spread and depth must be positive")
        if not -1.0 <= self.flow_bias <= 1.0:
            raise ValueError("flow_bias must lie in [-1, 1]")

    @property
    def step_count(self) -> int:
        return round(self.horizon_seconds / self.dt_seconds)


@dataclass(frozen=True, slots=True)
class MarketPoint:
    timestamp: float
    reference_price_ticks: int
    best_bid_ticks: int
    best_ask_ticks: int
    bid_size: int
    ask_size: int
    last_trade_price_ticks: int | None


@dataclass(frozen=True, slots=True)
class MarketPath:
    points: tuple[MarketPoint, ...]
    seed: int

    @property
    def horizon_seconds(self) -> float:
        return self.points[-1].timestamp if self.points else 0.0


def generate_market_path(config: PathConfig, seed: int) -> MarketPath:
    rng = Random(seed)
    points: list[MarketPoint] = []
    price = config.initial_price_ticks
    previous_price: int | None = None
    for index in range(config.step_count + 1):
        timestamp = round(index * config.dt_seconds, 12)
        if index > 0:
            shock = config.volatility_ticks * (config.dt_seconds**0.5) * rng.gauss(0.0, 1.0)
            drift = config.drift_ticks_per_second * config.dt_seconds
            price = max(config.half_spread_ticks + 2, round(price + drift + shock))

        bid_mean = config.depth * (1.0 + 0.25 * config.flow_bias)
        ask_mean = config.depth * (1.0 - 0.25 * config.flow_bias)
        bid_size = max(1, round(bid_mean + rng.gauss(0.0, max(1.0, bid_mean * 0.1))))
        ask_size = max(1, round(ask_mean + rng.gauss(0.0, max(1.0, ask_mean * 0.1))))
        points.append(
            MarketPoint(
                timestamp=timestamp,
                reference_price_ticks=price,
                best_bid_ticks=price - config.half_spread_ticks,
                best_ask_ticks=price + config.half_spread_ticks,
                bid_size=bid_size,
                ask_size=ask_size,
                last_trade_price_ticks=previous_price,
            )
        )
        previous_price = price
    return MarketPath(points=tuple(points), seed=seed)

