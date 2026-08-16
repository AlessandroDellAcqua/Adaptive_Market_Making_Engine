"""Shared data contracts for quote-generation strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class MarketObservation:
    timestamp: float
    best_bid_ticks: int
    best_ask_ticks: int
    bid_size: int
    ask_size: int
    volatility_ticks: float
    last_trade_price_ticks: float | None = None
    fair_price_ticks: float | None = None
    signed_trade_flow: float = 0.0
    cancellation_rate: float = 0.0
    queue_depletion_rate: float = 0.0
    toxicity_score: float = 0.0

    def __post_init__(self) -> None:
        if self.best_bid_ticks >= self.best_ask_ticks:
            raise ValueError("best bid must be below best ask")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("book sizes must be non-negative")
        if self.volatility_ticks < 0:
            raise ValueError("volatility cannot be negative")
        if not math.isfinite(self.signed_trade_flow):
            raise ValueError("signed trade flow must be finite")
        if self.cancellation_rate < 0 or self.queue_depletion_rate < 0:
            raise ValueError("flow rates cannot be negative")
        if not 0.0 <= self.toxicity_score <= 1.0:
            raise ValueError("toxicity score must lie in [0, 1]")

    @property
    def midprice_ticks(self) -> float:
        return (self.best_bid_ticks + self.best_ask_ticks) / 2.0

    @property
    def market_spread_ticks(self) -> int:
        return self.best_ask_ticks - self.best_bid_ticks

    @property
    def imbalance(self) -> float:
        total = self.bid_size + self.ask_size
        return 0.0 if total == 0 else (self.bid_size - self.ask_size) / total

    @property
    def microprice_ticks(self) -> float:
        total = self.bid_size + self.ask_size
        if total == 0:
            return self.midprice_ticks
        return (self.best_ask_ticks * self.bid_size + self.best_bid_ticks * self.ask_size) / total

    def reference_price(self, mode: str = "midprice") -> float:
        if mode == "midprice":
            return self.midprice_ticks
        if mode == "microprice":
            return self.microprice_ticks
        if mode == "fair_price":
            return (
                self.fair_price_ticks
                if self.fair_price_ticks is not None
                else self.midprice_ticks
            )
        raise ValueError(f"unknown reference mode: {mode}")


@dataclass(frozen=True, slots=True)
class AgentView:
    timestamp: float
    inventory: int
    cash: float
    q_target: int
    q_scale: float
    q_max: int
    drawdown: float = 0.0
    risk_status: str = "NORMAL"

    @property
    def scaled_inventory(self) -> float:
        if self.q_scale <= 0:
            raise ValueError("q_scale must be positive")
        return (self.inventory - self.q_target) / self.q_scale


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    half_width_ticks: float = 2.0
    min_half_width_ticks: float = 1.0
    max_half_width_ticks: float = 50.0
    max_order_size: int = 1
    inventory_kappa: float = 1.0
    gamma: float = 0.1
    fill_decay_k: float = 0.5
    horizon_seconds: float = 10.0
    volatility_floor_ticks: float = 0.01
    reference_mode: str = "midprice"
    imbalance_kappa: float = 0.0
    flow_kappa: float = 0.0
    volatility_width_kappa: float = 0.0
    toxicity_width_kappa: float = 0.0
    toxicity_size_scale: float = 0.0

    def __post_init__(self) -> None:
        if self.half_width_ticks <= 0:
            raise ValueError("half_width_ticks must be positive")
        if self.min_half_width_ticks <= 0:
            raise ValueError("min_half_width_ticks must be positive")
        if self.max_half_width_ticks < self.min_half_width_ticks:
            raise ValueError("max width must be at least min width")
        if self.max_order_size <= 0:
            raise ValueError("max_order_size must be positive")
        if self.inventory_kappa < 0 or self.gamma < 0:
            raise ValueError("risk parameters must be non-negative")
        if self.fill_decay_k <= 0:
            raise ValueError("fill_decay_k must be positive")
        if self.horizon_seconds < 0:
            raise ValueError("horizon_seconds must be non-negative")
        if (
            self.imbalance_kappa < 0
            or self.flow_kappa < 0
            or self.volatility_width_kappa < 0
            or self.toxicity_width_kappa < 0
            or self.toxicity_size_scale < 0
        ):
            raise ValueError("adaptive coefficients cannot be negative")


@dataclass(frozen=True, slots=True)
class RawQuote:
    reservation_price_ticks: float
    bid_price_ticks: float | None
    ask_price_ticks: float | None
    half_width_ticks: float
    bid_size: int | None = None
    ask_size: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuoteAction:
    strategy_name: str
    timestamp: float
    bid_price_ticks: int | None
    ask_price_ticks: int | None
    bid_size: int
    ask_size: int
    reservation_price_ticks: float
    half_width_ticks: float
    risk_status: str = "NORMAL"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def quoted_spread_ticks(self) -> int | None:
        if self.bid_price_ticks is None or self.ask_price_ticks is None:
            return None
        return self.ask_price_ticks - self.bid_price_ticks


class MarketMaker(Protocol):
    name: str

    def compute_raw_quote(self, observation: MarketObservation, state: AgentView) -> RawQuote:
        ...
