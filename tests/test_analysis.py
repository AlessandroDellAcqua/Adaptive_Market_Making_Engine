from lob_sim.agents import (
    InventorySkewMarketMaker,
    NaiveSymmetricMarketMaker,
    StrategyConfig,
)
from lob_sim.analysis.metrics import (
    normalized_run_metrics,
    paired_comparison,
    paired_comparisons,
    summarize_runs,
)
from lob_sim.analysis.selection import rank_candidates
from lob_sim.simulation import IntensityMarketSimulator, SimulationConfig
from lob_sim.synthetic import generate_market_path


def _paired_runs() -> list:
    config = SimulationConfig(
        horizon_seconds=2.0,
        dt_seconds=0.1,
        fill_intensity=8.0,
        q_max=5,
    )
    simulator = IntensityMarketSimulator(config)
    strategy_config = StrategyConfig(
        half_width_ticks=1.0,
        inventory_kappa=2.0,
        max_half_width_ticks=20.0,
    )
    strategies = [
        NaiveSymmetricMarketMaker(strategy_config),
        InventorySkewMarketMaker(strategy_config),
    ]
    runs = []
    for seed in range(4):
        path = generate_market_path(config, seed=seed)
        runs.extend(simulator.run(strategy, seed=seed, path=path) for strategy in strategies)
    return runs


def test_normalized_metrics_are_reported_in_run_summaries() -> None:
    runs = _paired_runs()
    normalized = normalized_run_metrics(runs[0])
    summary = summarize_runs(runs)

    assert normalized["total_traded_quantity"] >= 0.0
    assert normalized["turnover_notional_ticks"] >= 0.0
    assert 0.0 <= normalized["max_drawdown_pct"]
    assert "mean_pnl_per_traded_unit" in summary[runs[0].strategy_name]
    assert "expected_shortfall_p05" in summary[runs[0].strategy_name]


def test_paired_comparison_uses_common_random_number_seeds() -> None:
    runs = _paired_runs()
    comparison = paired_comparison(
        runs,
        baseline_strategy="naive_symmetric",
        candidate_strategy="inventory_skew",
        bootstrap_samples=250,
    )
    all_comparisons = paired_comparisons(
        runs,
        baseline_strategy="naive_symmetric",
        bootstrap_samples=250,
    )

    assert comparison["pairs"] == 4.0
    assert 0.0 <= comparison["probability_candidate_beats"] <= 1.0
    assert comparison["bootstrap_ci95_low"] <= comparison["mean_delta_pnl"]
    assert comparison["mean_delta_pnl"] <= comparison["bootstrap_ci95_high"]
    assert set(all_comparisons) == {"inventory_skew"}


def test_development_selector_ranks_candidates_without_holdout_access() -> None:
    runs = _paired_runs()
    grouped = {
        "naive_candidate": [run for run in runs if run.strategy_name == "naive_symmetric"],
        "inventory_candidate": [
            run for run in runs if run.strategy_name == "inventory_skew"
        ],
    }
    ranking = rank_candidates(grouped, inventory_penalty=0.05)

    assert len(ranking) == 2
    assert ranking[0]["selection_score"] >= ranking[1]["selection_score"]
    assert ranking[0]["paths"] == 4.0
