"""Compare market-making strategies using Project 1's event-driven LOB."""

from __future__ import annotations

import argparse
import csv
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
from lob_sim.analysis.metrics import compare_runs, paired_comparisons, summarize_runs
from lob_sim.analysis.plotting import write_comparison_svg
from lob_sim.external_execution import (
    ExternalLimitOrderBookMarketSimulator,
    generate_external_event_tape,
)
from lob_sim.provenance import write_experiment_manifest
from lob_sim.risk import RiskLimits
from lob_sim.simulation import SimulationConfig
from lob_sim.synthetic import generate_market_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulator-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "Limit_Order_Book_Simulator",
        help="Project 1 repository root or its src directory",
    )
    parser.add_argument("--regime", default="liquid")
    parser.add_argument(
        "--toxic-response-ticks",
        type=float,
        default=0.0,
        help="Causal next-step response to aggressive market events; zero disables it",
    )
    parser.add_argument("--paths", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/external_lob"))
    args = parser.parse_args()
    if args.paths <= 0:
        raise SystemExit("--paths must be positive")

    config = SimulationConfig(
        horizon_seconds=20.0,
        dt_seconds=0.1,
        volatility_ticks=5.0,
        half_spread_ticks=2,
        depth=10,
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
        max_order_size=strategy_config.max_order_size,
        min_half_width_ticks=strategy_config.min_half_width_ticks,
        max_half_width_ticks=strategy_config.max_half_width_ticks,
        max_market_spread_ticks=config.max_market_spread_ticks,
    )
    simulator = ExternalLimitOrderBookMarketSimulator(
        config,
        args.simulator_root,
        snapshot_depth=5,
        order_flow_config={"regime": args.regime},
    )

    all_runs = []
    representative = []
    for seed in range(args.paths):
        path = generate_market_path(config, seed)
        event_tape = (
            generate_external_event_tape(
                args.simulator_root,
                path,
                seed=seed,
                order_flow_config={"regime": args.regime},
                toxic_response_ticks=args.toxic_response_ticks,
            )
            if args.toxic_response_ticks
            else None
        )
        for strategy in strategies:
            run = simulator.run(
                strategy,
                seed=seed,
                path=path if event_tape is None else None,
                risk_limits=limits,
                external_event_tape=event_tape,
            )
            all_runs.append(run)
            if seed == 0:
                representative.append(run)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_runs(all_runs)
    manifest = write_experiment_manifest(
        args.output_dir / "experiment_manifest.json",
        experiment_name="external_lob_strategy_comparison",
        execution_mode="external_qr_platform_generated_or_toxic_replay",
        configuration={
            "simulation": asdict(config),
            "strategy": asdict(strategy_config),
            "adaptive_strategy": asdict(adaptive_config),
            "regime": args.regime,
            "toxic_response_ticks": args.toxic_response_ticks,
            "paths": args.paths,
        },
        strategy_names=[strategy.name for strategy in strategies],
        seeds=list(range(args.paths)),
        repository_root=Path(__file__).resolve().parents[1],
        external_root=args.simulator_root,
    )
    summary_payload = {
        "summary": summary,
        "paired_comparisons_vs_naive": paired_comparisons(
            all_runs,
            baseline_strategy="naive_symmetric",
        ),
        "manifest": manifest["format"],
    }
    (args.output_dir / "external_lob_comparison_summary.json").write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    rows = compare_runs(all_runs)
    with (args.output_dir / "external_lob_run_metrics.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write_comparison_svg(
        representative,
        args.output_dir / "external_lob_representative_comparison.svg",
    )
    print(json.dumps(summary_payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
