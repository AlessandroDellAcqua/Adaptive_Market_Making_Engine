import math

from lob_sim.accounting import Fill, Ledger
from lob_sim.core.models import Side


def test_round_trip_pnl_closes_with_spread_inventory_and_fees() -> None:
    ledger = Ledger(initial_cash=1_000.0, tick_value=0.01)
    ledger.mark(timestamp=0.0, reference_price_ticks=10_000)
    buy = Fill(
        fill_id="b1",
        order_id="o1",
        timestamp=0.0,
        side=Side.BUY,
        price_ticks=9_990,
        quantity=2,
        reference_price_ticks=10_000,
        fee=0.10,
    )
    ledger.apply_fill(buy)
    ledger.mark(timestamp=1.0, reference_price_ticks=10_010)
    sell = Fill(
        fill_id="a1",
        order_id="o2",
        timestamp=1.0,
        side=Side.SELL,
        price_ticks=10_020,
        quantity=2,
        reference_price_ticks=10_010,
        fee=0.10,
    )
    ledger.apply_fill(sell)

    assert ledger.inventory == 0
    assert math.isclose(ledger.pnl.spread_capture, 0.40)
    assert math.isclose(ledger.pnl.inventory_mark_to_market, 0.20)
    assert math.isclose(ledger.pnl.fees, 0.20)
    assert math.isclose(ledger.total_pnl, 0.40, abs_tol=1e-10)
    assert math.isclose(ledger.accounting_error, 0.0, abs_tol=1e-10)


def test_adverse_selection_is_a_separate_diagnostic_cost() -> None:
    ledger = Ledger(initial_cash=1_000.0, tick_value=0.01)
    ledger.mark(timestamp=0.0, reference_price_ticks=10_000)
    fill = Fill(
        fill_id="b1",
        order_id="o1",
        timestamp=0.0,
        side=Side.BUY,
        price_ticks=9_999,
        quantity=2,
        reference_price_ticks=10_000,
    )
    ledger.apply_fill(fill)
    cost = ledger.record_adverse_selection(fill, future_reference_price_ticks=9_990)

    assert math.isclose(cost, 0.20)
    assert math.isclose(ledger.pnl.adverse_selection_cost, 0.20)
    assert math.isclose(ledger.accounting_error, 0.0, abs_tol=1e-10)


def test_executable_bid_ask_marks_close_lob_wealth_identity() -> None:
    ledger = Ledger(initial_cash=1_000.0, tick_value=0.01)
    ledger.mark(
        timestamp=0.0,
        reference_price_ticks=10_000,
        executable_bid_ticks=9_999,
        executable_ask_ticks=10_001,
    )
    fill = Fill(
        fill_id="b1",
        order_id="o1",
        timestamp=0.0,
        side=Side.BUY,
        price_ticks=9_990,
        quantity=1,
        reference_price_ticks=10_000,
        executable_bid_ticks=9_999,
        executable_ask_ticks=10_001,
    )
    ledger.apply_fill(fill)

    assert math.isclose(ledger.wealth, 1_000.09, abs_tol=1e-10)
    assert math.isclose(ledger.accounting_error, 0.0, abs_tol=1e-10)
    assert ledger.pnl.executable_mark_adjustment < 0
