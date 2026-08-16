"""Compare the required strategies in the persistent synthetic L2 adapter."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from lob_sim.agents import (
    AvellanedaStoikovMarketMaker,
    InventorySkewMarketMaker,
    MicrostructureAdaptiveMarketMaker,
    NaiveSymmetricMarketMaker,
    StrategyConfig,
)
from lob_sim.analysis.metrics import paired_comparisons, summarize_runs
from lob_sim.analysis.plotting import write_comparison_svg
from lob_sim.lob_execution import PersistentLimitOrderBookMarketSimulator
from lob_sim.provenance import write_experiment_manifest
from lob_sim.risk import RiskLimits
from lob_sim.simulation import SimulationConfig
from lob_sim.synthetic import generate_market_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/lob"))
    args = parser.parse_args()
    if args.paths <= 0:
        raise SystemExit("--paths must be positive")

    config = SimulationConfig(
        horizon_seconds=20.0,
        dt_seconds=0.1,
        volatility_ticks=5.0,
        half_spread_ticks=2,
        depth=10,
        fill_intensity=4.0,
        fee_per_unit=0.005,
        q_max=10,
    )
    strategy_config = StrategyConfig(
        half_width_ticks=1.0,
        max_half_width_ticks=20.0,
        inventory_kappa=2.0,
        gamma=0.15,
        fill_decay_k=config.fill_decay_k,
        horizon_seconds=config.horizon_seconds,
    )
    adaptive_config = StrategyConfig(
        half_width_ticks=1.0,
        max_half_width_ticks=20.0,
        max_order_size=1,
        inventory_kappa=2.0,
        fill_decay_k=config.fill_decay_k,
        horizon_seconds=config.horizon_seconds,
        reference_mode="microprice",
        imbalance_kappa=1.5,
        flow_kappa=1.0,
        volatility_width_kappa=0.15,
        toxicity_width_kappa=2.0,
        toxicity_size_scale=0.8,
    )
    strategies = [
        NaiveSymmetricMarketMaker(strategy_config),
        InventorySkewMarketMaker(strategy_config),
        AvellanedaStoikovMarketMaker(strategy_config),
        MicrostructureAdaptiveMarketMaker(adaptive_config),
    ]
    limits = RiskLimits(
        q_max=config.q_max,
        max_order_size=1,
        min_half_width_ticks=1.0,
        max_half_width_ticks=20.0,
        max_market_spread_ticks=config.max_market_spread_ticks,
    )
    simulator = PersistentLimitOrderBookMarketSimulator(config)
    all_runs = []
    representative = []
    for seed in range(args.paths):
        path = generate_market_path(config, seed)
        for strategy in strategies:
            run = simulator.run(strategy, seed=seed, path=path, risk_limits=limits)
            all_runs.append(run)
            if seed == 0:
                representative.append(run)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_runs(all_runs)
    manifest = write_experiment_manifest(
        args.output_dir / "experiment_manifest.json",
        experiment_name="persistent_lob_strategy_comparison",
        execution_mode="persistent_synthetic_lob",
        configuration={
            "simulation": asdict(config),
            "strategy": asdict(strategy_config),
            "adaptive_strategy": asdict(adaptive_config),
        },
        strategy_names=[strategy.name for strategy in strategies],
        seeds=list(range(args.paths)),
        repository_root=Path(__file__).resolve().parents[1],
    )
    summary_payload = {
        "summary": summary,
        "paired_comparisons_vs_naive": paired_comparisons(
            all_runs,
            baseline_strategy="naive_symmetric",
        ),
        "manifest": manifest["format"],
    }
    (args.output_dir / "lob_comparison_summary.json").write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    write_comparison_svg(representative, args.output_dir / "lob_representative_comparison.svg")
    print(json.dumps(summary_payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
