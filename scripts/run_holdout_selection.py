"""Tune interpretable parameters on development paths and freeze a holdout."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path

from lob_sim.agents import (
    AvellanedaStoikovMarketMaker,
    InventorySkewMarketMaker,
    MicrostructureAdaptiveMarketMaker,
    NaiveSymmetricMarketMaker,
    StrategyConfig,
)
from lob_sim.analysis.metrics import paired_comparisons, summarize_runs
from lob_sim.analysis.selection import rank_candidates
from lob_sim.provenance import write_experiment_manifest
from lob_sim.risk import RiskLimits
from lob_sim.simulation import IntensityMarketSimulator, RunResult, SimulationConfig
from lob_sim.synthetic import generate_market_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-paths", type=int, default=30)
    parser.add_argument("--holdout-paths", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/holdout"))
    args = parser.parse_args()
    if args.dev_paths <= 0 or args.holdout_paths <= 0:
        raise SystemExit("development and holdout path counts must be positive")

    config = SimulationConfig(
        horizon_seconds=20.0,
        dt_seconds=0.1,
        volatility_ticks=5.0,
        fill_intensity=2.0,
        fill_decay_k=0.5,
        q_max=10,
    )
    base = StrategyConfig(
        half_width_ticks=2.0,
        min_half_width_ticks=1.0,
        max_half_width_ticks=20.0,
        max_order_size=1,
        inventory_kappa=2.0,
        gamma=0.15,
        fill_decay_k=config.fill_decay_k,
        horizon_seconds=config.horizon_seconds,
    )
    candidate_configs = {
        "inventory_skew": {
            f"inventory_kappa_{value:g}": replace(base, inventory_kappa=value)
            for value in (0.5, 1.0, 2.0, 4.0)
        },
        "avellaneda_stoikov": {
            f"gamma_{value:g}": replace(base, gamma=value)
            for value in (0.05, 0.15, 0.30)
        },
        "microstructure_adaptive": {
            f"imbalance_kappa_{value:g}": replace(
                base,
                reference_mode="microprice",
                imbalance_kappa=value,
                flow_kappa=1.0,
                volatility_width_kappa=0.15,
                toxicity_width_kappa=2.0,
                toxicity_size_scale=0.8,
            )
            for value in (0.5, 1.5, 3.0)
        },
    }
    limits = RiskLimits(
        q_max=config.q_max,
        max_order_size=base.max_order_size,
        min_half_width_ticks=base.min_half_width_ticks,
        max_half_width_ticks=base.max_half_width_ticks,
        max_market_spread_ticks=config.max_market_spread_ticks,
    )
    simulator = IntensityMarketSimulator(config)

    development_rankings: dict[str, list[dict[str, float | str]]] = {}
    selected: dict[str, tuple[str, StrategyConfig]] = {}
    for family, configs in candidate_configs.items():
        runs_by_candidate: dict[str, list[RunResult]] = {}
        for label, strategy_config in configs.items():
            strategy = _strategy_for_family(family, strategy_config)
            runs_by_candidate[label] = _run_paths(
                simulator,
                strategy,
                seeds=range(args.dev_paths),
                config=config,
                risk_limits=limits,
            )
        ranking = rank_candidates(runs_by_candidate, inventory_penalty=0.05)
        development_rankings[family] = ranking
        winner = str(ranking[0]["candidate"])
        selected[family] = (winner, configs[winner])

    holdout_strategies = [NaiveSymmetricMarketMaker(base)]
    holdout_labels = ["naive_symmetric"]
    for family, (_label, strategy_config) in selected.items():
        holdout_strategies.append(_strategy_for_family(family, strategy_config))
        holdout_labels.append(family)

    holdout_runs: list[RunResult] = []
    holdout_seeds = range(args.dev_paths, args.dev_paths + args.holdout_paths)
    for strategy, label in zip(holdout_strategies, holdout_labels, strict=True):
        for run in _run_paths(
            simulator,
            strategy,
            seeds=holdout_seeds,
            config=config,
            risk_limits=limits,
        ):
            holdout_runs.append(replace(run, strategy_name=label))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = write_experiment_manifest(
        args.output_dir / "experiment_manifest.json",
        experiment_name="development_holdout_parameter_selection",
        execution_mode="intensity_fill_model",
        configuration={
            "simulation": asdict(config),
            "inventory_penalty": 0.05,
            "development_paths": args.dev_paths,
            "holdout_paths": args.holdout_paths,
            "candidate_configs": {
                family: {
                    label: asdict(strategy_config)
                    for label, strategy_config in configs.items()
                }
                for family, configs in candidate_configs.items()
            },
        },
        strategy_names=holdout_labels,
        seeds=list(range(args.dev_paths + args.holdout_paths)),
        repository_root=Path(__file__).resolve().parents[1],
    )
    payload = {
        "manifest": manifest["format"],
        "development_rankings": development_rankings,
        "selected_parameters": {
            family: {"candidate": label, "config": asdict(strategy_config)}
            for family, (label, strategy_config) in selected.items()
        },
        "holdout_summary": summarize_runs(holdout_runs),
        "paired_comparisons_vs_naive": paired_comparisons(
            holdout_runs,
            baseline_strategy="naive_symmetric",
        ),
    }
    (args.output_dir / "holdout_selection_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


def _strategy_for_family(family: str, config: StrategyConfig):
    strategies = {
        "inventory_skew": InventorySkewMarketMaker,
        "avellaneda_stoikov": AvellanedaStoikovMarketMaker,
        "microstructure_adaptive": MicrostructureAdaptiveMarketMaker,
    }
    return strategies[family](config)


def _run_paths(
    simulator: IntensityMarketSimulator,
    strategy,
    *,
    seeds,
    config: SimulationConfig,
    risk_limits: RiskLimits,
) -> list[RunResult]:
    runs = []
    for seed in seeds:
        path = generate_market_path(config, seed)
        runs.append(simulator.run(strategy, seed=seed, path=path, risk_limits=risk_limits))
    return runs


if __name__ == "__main__":
    main()
