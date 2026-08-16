"""Immutable request/event models used by the matching engine.

Prices are integer ticks and quantities are integer units. Keeping the book in
discrete units makes price-time priority and replay exact instead of dependent
on floating-point comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Side(StrEnum):
    """Order side and inventory direction."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def inventory_delta(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A new order submitted to the book."""

    client_order_id: str
    side: Side
    quantity: int
    price_ticks: int | None = None
    order_type: OrderType = OrderType.LIMIT
    timestamp: float = 0.0
    owner: str = "external"

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise ValueError("client_order_id must be non-empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.timestamp < 0:
            raise ValueError("timestamp must be non-negative")
        if self.order_type is OrderType.LIMIT:
            if self.price_ticks is None or self.price_ticks <= 0:
                raise ValueError("limit orders require a positive price")
        elif self.price_ticks is not None:
            raise ValueError("market orders must not specify a limit price")


@dataclass(slots=True)
class Order:
    """A live or completed order with mutable remaining quantity."""

    order_id: int
    client_order_id: str
    side: Side
    quantity: int
    remaining: int
    price_ticks: int | None
    order_type: OrderType
    timestamp: float
    sequence: int
    owner: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class Trade:
    """A single match between an aggressor and a resting order."""

    trade_id: int
    timestamp: float
    price_ticks: int
    quantity: int
    aggressive_side: Side
    maker_order_id: int
    taker_order_id: int
    maker_owner: str
    taker_owner: str


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Result of submitting one request."""

    order_id: int
    accepted: bool
    remaining: int
    trades: tuple[Trade, ...]


@dataclass(frozen=True, slots=True)
class BookLevel:
    price_ticks: int
    quantity: int
    order_count: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Top-of-book snapshot used by agents and diagnostics."""

    timestamp: float
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    bid_size: int
    ask_size: int
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    last_trade_price_ticks: int | None = None

    @property
    def spread_ticks(self) -> int | None:
        if self.best_bid_ticks is None or self.best_ask_ticks is None:
            return None
        return self.best_ask_ticks - self.best_bid_ticks

    @property
    def midprice_ticks(self) -> float | None:
        if self.best_bid_ticks is None or self.best_ask_ticks is None:
            return None
        return (self.best_bid_ticks + self.best_ask_ticks) / 2.0

    @property
    def imbalance(self) -> float:
        total = self.bid_size + self.ask_size
        return 0.0 if total == 0 else (self.bid_size - self.ask_size) / total

    @property
    def microprice_ticks(self) -> float | None:
        if self.best_bid_ticks is None or self.best_ask_ticks is None:
            return None
        total = self.bid_size + self.ask_size
        if total == 0:
            return self.midprice_ticks
        return (self.best_ask_ticks * self.bid_size + self.best_bid_ticks * self.ask_size) / total
