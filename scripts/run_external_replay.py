"""Replay one persisted Project 1 event tape across four strategies."""

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
from lob_sim.data.external_tape import ExternalEventTape
from lob_sim.external_execution import ExternalLimitOrderBookMarketSimulator
from lob_sim.provenance import write_experiment_manifest
from lob_sim.risk import RiskLimits
from lob_sim.simulation import SimulationConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulator-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "Limit_Order_Book_Simulator",
        help="Project 1 repository root or its src directory",
    )
    parser.add_argument(
        "--event-tape",
        type=Path,
        required=True,
        help="Persisted external event-tape JSON file",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/external_replay")
    )
    args = parser.parse_args()

    tape = ExternalEventTape.read(args.event_tape)
    points = tape.market_path.points
    dt_seconds = points[1].timestamp - points[0].timestamp
    config = SimulationConfig(
        horizon_seconds=tape.market_path.horizon_seconds,
        dt_seconds=dt_seconds,
        volatility_ticks=5.0,
        half_spread_ticks=max(1, (points[0].best_ask_ticks - points[0].best_bid_ticks) // 2),
        depth=max(points[0].bid_size, points[0].ask_size),
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
    )
    all_runs = [
        simulator.run(
            strategy,
            seed=tape.market_path.seed,
            external_event_tape=tape,
            risk_limits=limits,
        )
        for strategy in strategies
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_runs(all_runs)
    manifest = write_experiment_manifest(
        args.output_dir / "experiment_manifest.json",
        experiment_name="external_lob_persisted_replay",
        execution_mode="external_qr_platform_persisted_replay",
        configuration={
            "simulation": asdict(config),
            "strategy": asdict(strategy_config),
            "adaptive_strategy": asdict(adaptive_config),
            "event_tape": str(args.event_tape.name),
        },
        strategy_names=[strategy.name for strategy in strategies],
        seeds=[tape.market_path.seed],
        repository_root=Path(__file__).resolve().parents[1],
        external_root=args.simulator_root,
        input_digests={"event_tape": tape.digest},
    )
    summary_payload = {
        "event_tape_digest": tape.digest,
        "summary": summary,
        "paired_comparisons_vs_naive": paired_comparisons(
            all_runs,
            baseline_strategy="naive_symmetric",
        ),
        "manifest": manifest["format"],
    }
    (args.output_dir / "replay_comparison_summary.json").write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    rows = compare_runs(all_runs)
    with (args.output_dir / "replay_run_metrics.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write_comparison_svg(
        all_runs,
        args.output_dir / "replay_representative_comparison.svg",
    )
    print(json.dumps(summary_payload, indent=2))


if __name__ == "__main__":
    main()
