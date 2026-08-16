from lob_sim.agents import NaiveSymmetricMarketMaker, StrategyConfig
from lob_sim.lob_execution import (
    LimitOrderBookMarketSimulator,
    PersistentLimitOrderBookMarketSimulator,
)
from lob_sim.risk import RiskLimits
from lob_sim.simulation import SimulationConfig


def test_strategy_orders_are_matched_by_the_real_book() -> None:
    config = SimulationConfig(
        horizon_seconds=1.0,
        dt_seconds=0.1,
        half_spread_ticks=2,
        depth=10,
        fill_intensity=10.0,
        fee_per_unit=0.0,
    )
    strategy = NaiveSymmetricMarketMaker(StrategyConfig(half_width_ticks=1.0))
    run = LimitOrderBookMarketSimulator(config).run(strategy, seed=2)

    assert run.metrics["fill_count"] > 0.0
    assert all(fill.price_ticks > 0 for fill in run.fills)
    assert abs(run.metrics["accounting_error"]) < 1e-8
    assert any(event["kind"] == "book_snapshot" for event in run.event_tape)


def test_best_price_quotes_wait_behind_background_queue() -> None:
    config = SimulationConfig(
        horizon_seconds=1.0,
        dt_seconds=0.1,
        half_spread_ticks=2,
        depth=100,
        fill_intensity=100.0,
        fee_per_unit=0.0,
    )
    strategy = NaiveSymmetricMarketMaker(StrategyConfig(half_width_ticks=2.0))
    limits = RiskLimits(q_max=10, max_order_size=1)
    run = LimitOrderBookMarketSimulator(config).run(strategy, seed=4, risk_limits=limits)

    assert run.metrics["fill_count"] == 0.0
    assert run.metrics["final_inventory"] == 0.0


def test_persistent_book_replay_is_deterministic_and_accounts_for_repricing() -> None:
    config = SimulationConfig(
        horizon_seconds=1.0,
        dt_seconds=0.1,
        half_spread_ticks=2,
        depth=10,
        fill_intensity=10.0,
        volatility_ticks=8.0,
        fee_per_unit=0.0,
    )
    strategy = NaiveSymmetricMarketMaker(StrategyConfig(half_width_ticks=1.0))
    simulator = PersistentLimitOrderBookMarketSimulator(config)

    first = simulator.run(strategy, seed=12)
    second = simulator.run(strategy, seed=12)

    assert first.fills == second.fills
    assert first.metrics == second.metrics
    assert first.event_tape.to_json() == second.event_tape.to_json()
    assert any(event["kind"] == "background_replenishment" for event in first.event_tape)
    assert abs(first.metrics["accounting_error"]) < 1e-8
