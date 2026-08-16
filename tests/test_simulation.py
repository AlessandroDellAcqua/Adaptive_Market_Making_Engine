import json
import math

from lob_sim.agents import (
    AvellanedaStoikovMarketMaker,
    InventorySkewMarketMaker,
    NaiveSymmetricMarketMaker,
    StrategyConfig,
)
from lob_sim.analysis.metrics import summarize_runs
from lob_sim.simulation import IntensityMarketSimulator, SimulationConfig, fill_probability
from lob_sim.synthetic import generate_market_path


def test_fixed_seed_replays_actions_fills_and_pnl() -> None:
    config = SimulationConfig(horizon_seconds=2.0, dt_seconds=0.1)
    simulator = IntensityMarketSimulator(config)
    strategy = NaiveSymmetricMarketMaker(StrategyConfig())
    path = generate_market_path(config, seed=17)

    first = simulator.run(strategy, seed=17, path=path)
    second = simulator.run(strategy, seed=17, path=path)

    assert first.metrics == second.metrics
    assert first.quotes == second.quotes
    assert first.fills == second.fills
    assert json.loads(first.event_tape.to_json()) == json.loads(second.event_tape.to_json())
    assert math.isclose(first.metrics["accounting_error"], 0.0, abs_tol=1e-8)


def test_required_strategies_finish_flat_and_produce_comparable_metrics() -> None:
    config = SimulationConfig(horizon_seconds=3.0, dt_seconds=0.1)
    simulator = IntensityMarketSimulator(config)
    strategy_config = StrategyConfig(
        inventory_kappa=2.0,
        gamma=0.15,
        horizon_seconds=config.horizon_seconds,
        fill_decay_k=config.fill_decay_k,
    )
    strategies = [
        NaiveSymmetricMarketMaker(strategy_config),
        InventorySkewMarketMaker(strategy_config),
        AvellanedaStoikovMarketMaker(strategy_config),
    ]
    path = generate_market_path(config, seed=11)
    runs = [simulator.run(strategy, seed=11, path=path) for strategy in strategies]

    assert {run.strategy_name for run in runs} == {
        "naive_symmetric",
        "inventory_skew",
        "avellaneda_stoikov",
    }
    assert all(run.metrics["final_inventory"] == 0.0 for run in runs)
    assert all(abs(run.metrics["accounting_error"]) < 1e-8 for run in runs)
    assert set(summarize_runs(runs)) == {
        "naive_symmetric",
        "inventory_skew",
        "avellaneda_stoikov",
    }


def test_fill_probability_decreases_with_distance_and_queue() -> None:
    close = fill_probability(
        distance=1.0,
        interval_seconds=0.1,
        baseline_intensity=2.0,
        decay_k=0.5,
        queue_ahead=0.0,
        queue_decay_rho=1.0,
        queue_scale=10.0,
    )
    far = fill_probability(
        distance=3.0,
        interval_seconds=0.1,
        baseline_intensity=2.0,
        decay_k=0.5,
        queue_ahead=0.0,
        queue_decay_rho=1.0,
        queue_scale=10.0,
    )
    queued = fill_probability(
        distance=1.0,
        interval_seconds=0.1,
        baseline_intensity=2.0,
        decay_k=0.5,
        queue_ahead=20.0,
        queue_decay_rho=1.0,
        queue_scale=10.0,
    )

    assert close > far > 0.0
    assert close > queued


def test_latency_delays_execution_and_can_remove_all_active_quotes() -> None:
    config = SimulationConfig(horizon_seconds=1.0, dt_seconds=0.1, latency_seconds=2.0)
    simulator = IntensityMarketSimulator(config)
    strategy = NaiveSymmetricMarketMaker(StrategyConfig())
    run = simulator.run(strategy, seed=3)

    assert run.metrics["fill_count"] == 0.0
    assert all(event["kind"] != "fill" for event in run.event_tape)
