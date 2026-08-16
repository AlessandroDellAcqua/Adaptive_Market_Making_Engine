"""A deterministic price-time-priority limit order book."""

from __future__ import annotations

from collections import deque

from lob_sim.core.models import (
    BookLevel,
    BookSnapshot,
    ExecutionReport,
    Order,
    OrderRequest,
    OrderType,
    Side,
    Trade,
)


class LimitOrderBook:
    """Single-instrument limit order book with deterministic matching.

    Incoming orders match at the resting order's price. Resting orders are
    ordered first by price and then by a monotonically increasing sequence
    number. The book never uses floating-point prices.
    """

    def __init__(self) -> None:
        self._bids: dict[int, deque[int]] = {}
        self._asks: dict[int, deque[int]] = {}
        self._orders: dict[int, Order] = {}
        self._next_order_id = 1
        self._next_trade_id = 1
        self._next_sequence = 1
        self._last_trade_price: int | None = None

    @property
    def active_order_count(self) -> int:
        return len(self._orders)

    def submit(self, request: OrderRequest) -> ExecutionReport:
        order = Order(
            order_id=self._next_order_id,
            client_order_id=request.client_order_id,
            side=request.side,
            quantity=request.quantity,
            remaining=request.quantity,
            price_ticks=request.price_ticks,
            order_type=request.order_type,
            timestamp=request.timestamp,
            sequence=self._next_sequence,
            owner=request.owner,
        )
        self._next_order_id += 1
        self._next_sequence += 1

        trades = self._match(order, request.price_ticks)
        if order.remaining > 0 and request.order_type is OrderType.LIMIT:
            self._rest(order)
        else:
            order.active = False

        return ExecutionReport(
            order_id=order.order_id,
            accepted=True,
            remaining=order.remaining,
            trades=tuple(trades),
        )

    def cancel(self, order_id: int) -> bool:
        """Cancel a live order and return whether it existed."""

        order = self._orders.pop(order_id, None)
        if order is None:
            return False
        book = self._bids if order.side is Side.BUY else self._asks
        queue = book[order.price_ticks]  # type: ignore[index]
        queue.remove(order_id)
        if not queue:
            del book[order.price_ticks]  # type: ignore[index]
        order.active = False
        return True

    def replace(
        self,
        order_id: int,
        *,
        price_ticks: int,
        quantity: int,
        timestamp: float,
    ) -> ExecutionReport:
        """Cancel then resubmit an order, resetting time priority."""

        old = self._orders.get(order_id)
        if old is None:
            raise KeyError(f"unknown active order {order_id}")
        self.cancel(order_id)
        return self.submit(
            OrderRequest(
                client_order_id=old.client_order_id,
                side=old.side,
                quantity=quantity,
                price_ticks=price_ticks,
                order_type=OrderType.LIMIT,
                timestamp=timestamp,
                owner=old.owner,
            )
        )

    def get_order(self, order_id: int) -> Order | None:
        return self._orders.get(order_id)

    def snapshot(self, timestamp: float = 0.0, depth: int = 5) -> BookSnapshot:
        if depth <= 0:
            raise ValueError("depth must be positive")
        bid_levels = self._levels(self._bids, descending=True, depth=depth)
        ask_levels = self._levels(self._asks, descending=False, depth=depth)
        return BookSnapshot(
            timestamp=timestamp,
            best_bid_ticks=bid_levels[0].price_ticks if bid_levels else None,
            best_ask_ticks=ask_levels[0].price_ticks if ask_levels else None,
            bid_size=bid_levels[0].quantity if bid_levels else 0,
            ask_size=ask_levels[0].quantity if ask_levels else 0,
            bids=tuple(bid_levels),
            asks=tuple(ask_levels),
            last_trade_price_ticks=self._last_trade_price,
        )

    def assert_invariants(self) -> None:
        """Raise `AssertionError` if internal book invariants are violated."""

        seen: set[int] = set()
        for side_book in (self._bids, self._asks):
            for price, queue in side_book.items():
                assert price > 0
                assert queue
                for order_id in queue:
                    assert order_id not in seen
                    seen.add(order_id)
                    order = self._orders[order_id]
                    assert order.active
                    assert order.price_ticks == price
                    assert order.remaining > 0
        assert seen == set(self._orders)
        if self._bids and self._asks:
            assert max(self._bids) < min(self._asks)

    def _match(self, incoming: Order, limit_price: int | None) -> list[Trade]:
        trades: list[Trade] = []
        while incoming.remaining > 0:
            resting = self._best_resting(incoming.side)
            if resting is None:
                break
            if limit_price is not None:
                crosses = (
                    resting.price_ticks <= limit_price
                    if incoming.side is Side.BUY
                    else resting.price_ticks >= limit_price
                )
                if not crosses:
                    break

            quantity = min(incoming.remaining, resting.remaining)
            trade = Trade(
                trade_id=self._next_trade_id,
                timestamp=max(incoming.timestamp, resting.timestamp),
                price_ticks=resting.price_ticks,  # type: ignore[arg-type]
                quantity=quantity,
                aggressive_side=incoming.side,
                maker_order_id=resting.order_id,
                taker_order_id=incoming.order_id,
                maker_owner=resting.owner,
                taker_owner=incoming.owner,
            )
            self._next_trade_id += 1
            trades.append(trade)
            self._last_trade_price = trade.price_ticks
            incoming.remaining -= quantity
            resting.remaining -= quantity
            if resting.remaining == 0:
                self.cancel(resting.order_id)
        return trades

    def _best_resting(self, incoming_side: Side) -> Order | None:
        book = self._asks if incoming_side is Side.BUY else self._bids
        if not book:
            return None
        price = min(book) if incoming_side is Side.BUY else max(book)
        return self._orders[book[price][0]]

    def _rest(self, order: Order) -> None:
        assert order.price_ticks is not None
        book = self._bids if order.side is Side.BUY else self._asks
        book.setdefault(order.price_ticks, deque()).append(order.order_id)
        self._orders[order.order_id] = order

    def _levels(
        self, book: dict[int, deque[int]], *, descending: bool, depth: int
    ) -> list[BookLevel]:
        levels: list[BookLevel] = []
        for price in sorted(book, reverse=descending)[:depth]:
            queue = book[price]
            quantity = sum(self._orders[order_id].remaining for order_id in queue)
            levels.append(
                BookLevel(price_ticks=price, quantity=quantity, order_count=len(queue))
            )
        return levels
