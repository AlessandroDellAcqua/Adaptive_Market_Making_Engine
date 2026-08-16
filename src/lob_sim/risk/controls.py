"""Risk gates shared by every quoting strategy."""

from __future__ import annotations

import math
from dataclasses import dataclass

from lob_sim.agents.interface import AgentView, MarketObservation, QuoteAction, RawQuote


@dataclass(frozen=True, slots=True)
class RiskLimits:
    q_max: int = 10
    max_order_size: int = 1
    min_half_width_ticks: float = 1.0
    max_half_width_ticks: float = 50.0
    max_market_spread_ticks: int = 100
    max_drawdown: float = math.inf
    volatility_limit_ticks: float = math.inf
    reduced_inventory_fraction: float = 0.8
    reduced_order_size_factor: float = 0.5
    reduced_width_multiplier: float = 1.25

    def __post_init__(self) -> None:
        if self.q_max <= 0:
            raise ValueError("q_max must be positive")
        if self.max_order_size <= 0:
            raise ValueError("max_order_size must be positive")
        if self.min_half_width_ticks <= 0:
            raise ValueError("minimum half-width must be positive")
        if self.max_half_width_ticks < self.min_half_width_ticks:
            raise ValueError("maximum width must be at least minimum width")
        if self.max_market_spread_ticks <= 0:
            raise ValueError("max_market_spread_ticks must be positive")
        if not 0.0 < self.reduced_inventory_fraction < 1.0:
            raise ValueError("reduced inventory fraction must lie in (0, 1)")
        if not 0.0 < self.reduced_order_size_factor <= 1.0:
            raise ValueError("reduced order size factor must lie in (0, 1]")
        if self.reduced_width_multiplier < 1.0:
            raise ValueError("reduced width multiplier must be at least one")


def sanitize_quote(
    raw: RawQuote,
    *,
    observation: MarketObservation,
    state: AgentView,
    limits: RiskLimits,
    active_bid_qty: int = 0,
    active_ask_qty: int = 0,
    strategy_name: str,
) -> QuoteAction:
    """Round, validate, and risk-gate a strategy's continuous quote."""

    halted = (
        state.risk_status in {"HALTED", "LIQUIDATION"}
        or observation.market_spread_ticks > limits.max_market_spread_ticks
        or observation.volatility_ticks > limits.volatility_limit_ticks
        or state.drawdown >= limits.max_drawdown
    )
    if halted:
        return QuoteAction(
            strategy_name=strategy_name,
            timestamp=observation.timestamp,
            bid_price_ticks=None,
            ask_price_ticks=None,
            bid_size=0,
            ask_size=0,
            reservation_price_ticks=raw.reservation_price_ticks,
            half_width_ticks=raw.half_width_ticks,
            risk_status="HALTED",
            metadata={**raw.metadata, "halt_reason": "risk_gate"},
        )

    reduced = state.risk_status == "REDUCED"
    width_multiplier = limits.reduced_width_multiplier if reduced else 1.0
    half_width = min(
        max(raw.half_width_ticks * width_multiplier, limits.min_half_width_ticks),
        limits.max_half_width_ticks,
    )
    bid = math.floor(raw.bid_price_ticks) if raw.bid_price_ticks is not None else None
    ask = math.ceil(raw.ask_price_ticks) if raw.ask_price_ticks is not None else None

    if bid is not None:
        bid = min(bid, observation.best_ask_ticks - 1)
        bid = max(bid, 1)
    if ask is not None:
        ask = max(ask, observation.best_bid_ticks + 1)

    if bid is not None and ask is not None and bid >= ask:
        ask = bid + 1

    size = limits.max_order_size
    if reduced:
        size = max(1, math.floor(size * limits.reduced_order_size_factor))
    bid_size = size if raw.bid_size is None else max(0, min(size, raw.bid_size))
    ask_size = size if raw.ask_size is None else max(0, min(size, raw.ask_size))

    # Existing live orders are included in the worst-case inventory check.
    if bid is not None and state.inventory + active_bid_qty + bid_size > limits.q_max:
        bid = None
        bid_size = 0
    if ask is not None and state.inventory - active_ask_qty - ask_size < -limits.q_max:
        ask = None
        ask_size = 0

    # At the hard boundary, only the inventory-reducing side may remain.
    if state.inventory >= limits.q_max:
        bid = None
        bid_size = 0
    if state.inventory <= -limits.q_max:
        ask = None
        ask_size = 0

    return QuoteAction(
        strategy_name=strategy_name,
        timestamp=observation.timestamp,
        bid_price_ticks=bid,
        ask_price_ticks=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        reservation_price_ticks=raw.reservation_price_ticks,
        half_width_ticks=half_width,
        risk_status="REDUCED" if reduced else "NORMAL",
        metadata=raw.metadata,
    )
