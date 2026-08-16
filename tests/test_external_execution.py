from pathlib import Path
from statistics import mean

import pytest

from lob_sim.agents import NaiveSymmetricMarketMaker, StrategyConfig
from lob_sim.data.external_tape import ExternalEventTape
from lob_sim.external_execution import (
    ExternalLimitOrderBookMarketSimulator,
    generate_external_event_tape,
    load_external_lob_modules,
)
from lob_sim.risk import RiskLimits
from lob_sim.simulation import SimulationConfig
from lob_sim.synthetic import generate_market_path

EXTERNAL_ROOT = Path(__file__).resolve().parents[2] / "Limit_Order_Book_Simulator"
pytestmark = pytest.mark.skipif(
    not (EXTERNAL_ROOT / "src" / "qr_platform" / "engine.py").is_file(),
    reason="Project 1 order-book simulator is not available beside this repository",
)


def test_external_engine_contract_is_price_time_priority() -> None:
    modules = load_external_lob_modules(EXTERNAL_ROOT)
    book = modules.LimitOrderBook()
    book.process(
        modules.Event("limit", 1, side="sell", order_id="ask-1", price=101, quantity=3)
    )
    book.process(
        modules.Event("limit", 2, side="sell", order_id="ask-2", price=101, quantity=4)
    )
    trades = book.process(
        modules.Event("market", 3, side="buy", order_id="taker-1", quantity=5)
    )

    assert [(trade.resting_order_id, trade.quantity) for trade in trades] == [
        ("ask-1", 3),
        ("ask-2", 2),
    ]
    assert book.orders["ask-2"].quantity == 2
    assert book.orders["ask-2"].quantity_ahead_at_entry == 3
    book.audit()


def test_market_maker_runs_through_external_event_driven_book() -> None:
    config = SimulationConfig(
        horizon_seconds=1.0,
        dt_seconds=0.1,
        volatility_ticks=5.0,
        half_spread_ticks=2,
        depth=10,
        fee_per_unit=0.0,
        q_max=10,
    )
    strategy = NaiveSymmetricMarketMaker(StrategyConfig(half_width_ticks=1.0))
    limits = RiskLimits(q_max=10, max_order_size=1)
    simulator = ExternalLimitOrderBookMarketSimulator(config, EXTERNAL_ROOT)

    first = simulator.run(strategy, seed=3, risk_limits=limits)
    second = simulator.run(strategy, seed=3, risk_limits=limits)

    assert first.metrics == second.metrics
    assert first.fills == second.fills
    assert first.event_tape.to_json() == second.event_tape.to_json()
    assert first.metrics["external_event_count"] >= config.step_count
    assert first.metrics["external_trade_count"] >= first.metrics["fill_count"]
    assert first.metrics["mean_queue_ahead_at_entry"] >= 0.0
    assert first.metrics["max_queue_ahead_at_entry"] >= first.metrics["mean_queue_ahead_at_entry"]
    assert "executable_mark_adjustment" in first.metrics
    assert all(
        fill.executable_bid_ticks is not None and fill.executable_ask_ticks is not None
        for fill in first.fills
    )
    assert first.metrics["final_inventory"] == 0.0
    assert abs(first.metrics["accounting_error"]) < 1e-8
    assert any(event["kind"] == "external_event" for event in first.event_tape)
    assert any(event["kind"] == "liquidation" for event in first.event_tape)


def test_persisted_external_tape_replays_byte_for_byte() -> None:
    config = SimulationConfig(
        horizon_seconds=1.0,
        dt_seconds=0.1,
        volatility_ticks=5.0,
        half_spread_ticks=2,
        depth=10,
        fee_per_unit=0.0,
    )
    path = generate_market_path(config, seed=17)
    tape = generate_external_event_tape(EXTERNAL_ROOT, path, seed=17)
    restored = ExternalEventTape.from_json(tape.to_json())
    strategy = NaiveSymmetricMarketMaker(StrategyConfig(half_width_ticks=1.0))
    simulator = ExternalLimitOrderBookMarketSimulator(config, EXTERNAL_ROOT)

    first = simulator.run(strategy, seed=17, external_event_tape=tape)
    second = simulator.run(strategy, seed=17, external_event_tape=restored)

    assert tape.digest == restored.digest
    assert first.metrics == second.metrics
    assert first.fills == second.fills
    metadata = next(event for event in first.event_tape if event["kind"] == "external_tape")
    assert metadata["mode"] == "replay"
    assert metadata["digest"] == tape.digest


def test_persisted_external_tape_rejects_corrupted_event_schema() -> None:
    config = SimulationConfig(horizon_seconds=1.0, dt_seconds=0.1)
    path = generate_market_path(config, seed=19)
    tape = generate_external_event_tape(EXTERNAL_ROOT, path, seed=19)
    payload = tape.to_dict()
    payload["events"][0]["event_type"] = "corrupted"

    with pytest.raises(ValueError, match="invalid event_type"):
        ExternalEventTape.from_dict(payload)


def test_persisted_external_tape_rejects_duplicate_event_ids() -> None:
    config = SimulationConfig(horizon_seconds=1.0, dt_seconds=0.1)
    path = generate_market_path(config, seed=23)
    tape = generate_external_event_tape(EXTERNAL_ROOT, path, seed=23)
    payload = tape.to_dict()
    payload["events"][1]["event_id"] = payload["events"][0]["event_id"]

    with pytest.raises(ValueError, match="duplicate event_id"):
        ExternalEventTape.from_dict(payload)


def test_toxic_response_has_known_causal_direction() -> None:
    config = SimulationConfig(
        horizon_seconds=5.0,
        dt_seconds=0.1,
        volatility_ticks=2.0,
        half_spread_ticks=2,
        depth=10,
    )
    path = generate_market_path(config, seed=21)
    tape = generate_external_event_tape(
        EXTERNAL_ROOT,
        path,
        seed=21,
        toxic_response_ticks=8.0,
    )
    moves = {
        "buy": [],
        "sell": [],
    }
    for index, event in enumerate(tape.events):
        if event["event_type"] == "market":
            side = event["side"]
            moves[side].append(
                tape.market_path.points[index + 1].reference_price_ticks
                - tape.market_path.points[index].reference_price_ticks
            )

    assert moves["buy"] and moves["sell"]
    assert mean(moves["buy"]) > mean(moves["sell"])
