from lob_sim.core.book import LimitOrderBook
from lob_sim.core.models import OrderRequest, OrderType, Side


def test_price_time_priority_and_partial_fill() -> None:
    book = LimitOrderBook()
    first = book.submit(
        OrderRequest("bid-a", Side.BUY, quantity=2, price_ticks=100, owner="a")
    )
    second = book.submit(
        OrderRequest("bid-b", Side.BUY, quantity=3, price_ticks=100, owner="b")
    )

    report = book.submit(
        OrderRequest(
            "sell-market",
            Side.SELL,
            quantity=4,
            order_type=OrderType.MARKET,
            owner="taker",
        )
    )

    assert first.order_id != second.order_id
    assert [(trade.maker_order_id, trade.quantity) for trade in report.trades] == [
        (first.order_id, 2),
        (second.order_id, 2),
    ]
    assert book.get_order(second.order_id).remaining == 1  # type: ignore[union-attr]
    snapshot = book.snapshot()
    assert snapshot.best_bid_ticks == 100
    assert snapshot.bid_size == 1
    book.assert_invariants()


def test_limit_order_only_matches_when_it_crosses() -> None:
    book = LimitOrderBook()
    book.submit(OrderRequest("ask", Side.SELL, quantity=2, price_ticks=101))

    passive = book.submit(OrderRequest("bid", Side.BUY, quantity=1, price_ticks=100))
    assert passive.trades == ()
    assert passive.remaining == 1
    assert book.snapshot().spread_ticks == 1

    aggressive = book.submit(OrderRequest("bid-cross", Side.BUY, quantity=1, price_ticks=101))
    assert len(aggressive.trades) == 1
    assert aggressive.trades[0].price_ticks == 101
    assert book.snapshot().best_ask_ticks == 101
    book.assert_invariants()


def test_cancel_and_replace_reset_priority() -> None:
    book = LimitOrderBook()
    first = book.submit(OrderRequest("first", Side.BUY, quantity=1, price_ticks=100))
    second = book.submit(OrderRequest("second", Side.BUY, quantity=1, price_ticks=100))

    assert book.cancel(first.order_id)
    replacement = book.replace(
        second.order_id,
        price_ticks=99,
        quantity=2,
        timestamp=1.0,
    )
    assert replacement.remaining == 2
    snapshot = book.snapshot()
    assert snapshot.best_bid_ticks == 99
    assert snapshot.bid_size == 2
    book.assert_invariants()

