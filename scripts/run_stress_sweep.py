"""Run latency/volatility stress sweeps for all four strategies."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path
from statistics import mean, stdev

from lob_sim.agents import (
    AvellanedaStoikovMarketMaker,
    InventorySkewMarketMaker,
    MicrostructureAdaptiveMarketMaker,
    NaiveSymmetricMarketMaker,
    StrategyConfig,
)
from lob_sim.provenance import write_experiment_manifest
from lob_sim.risk import RiskLimits
from lob_sim.simulation import IntensityMarketSimulator, SimulationConfig
from lob_sim.synthetic import generate_market_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/stress"))
    args = parser.parse_args()
    if args.paths <= 0:
        raise SystemExit("--paths must be positive")

    volatilities = (2.0, 5.0, 10.0)
    latencies = (0.0, 0.1, 0.3, 0.5)
    rows: list[dict[str, float | str]] = []
    strategy_config = StrategyConfig(
        half_width_ticks=2.0,
        max_half_width_ticks=20.0,
        inventory_kappa=2.0,
        gamma=0.15,
        fill_decay_k=0.5,
        horizon_seconds=20.0,
    )
    adaptive_config = StrategyConfig(
        half_width_ticks=2.0,
        max_half_width_ticks=20.0,
        inventory_kappa=2.0,
        fill_decay_k=0.5,
        horizon_seconds=20.0,
        reference_mode="microprice",
        imbalance_kappa=1.5,
        flow_kappa=1.0,
        volatility_width_kappa=0.15,
        toxicity_width_kappa=2.0,
        toxicity_size_scale=0.8,
    )

    for volatility in volatilities:
        for latency in latencies:
            config = SimulationConfig(
                horizon_seconds=20.0,
                dt_seconds=0.1,
                volatility_ticks=volatility,
                latency_seconds=latency,
                fill_intensity=2.0,
                fill_decay_k=0.5,
                q_max=10,
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
            simulator = IntensityMarketSimulator(config)
            for seed in range(args.paths):
                path = generate_market_path(config, seed)
                for strategy in strategies:
                    run = simulator.run(strategy, seed=seed, path=path, risk_limits=limits)
                    rows.append(
                        {
                            "strategy": strategy.name,
                            "seed": float(seed),
                            "volatility_ticks": volatility,
                            "latency_seconds": latency,
                            "final_pnl": run.metrics["final_pnl"],
                            "max_abs_inventory": run.metrics["max_abs_inventory"],
                            "max_drawdown": run.metrics["max_drawdown"],
                            "fill_rate": run.metrics["fill_rate"],
                            "mean_abs_inventory": run.metrics["mean_abs_inventory"],
                        }
                    )

    summary: dict[str, dict[str, float]] = {}
    groups: dict[tuple[str, float, float], list[dict[str, float | str]]] = {}
    for row in rows:
        key = (str(row["strategy"]), float(row["volatility_ticks"]), float(row["latency_seconds"]))
        groups.setdefault(key, []).append(row)
    for (strategy, volatility, latency), group in groups.items():
        pnls = [float(row["final_pnl"]) for row in group]
        summary[_key(strategy, volatility, latency)] = {
            "strategy": strategy,
            "volatility_ticks": volatility,
            "latency_seconds": latency,
            "paths": float(len(group)),
            "mean_final_pnl": mean(pnls),
            "std_final_pnl": stdev(pnls) if len(pnls) > 1 else 0.0,
            "mean_max_abs_inventory": mean(float(row["max_abs_inventory"]) for row in group),
            "mean_max_drawdown": mean(float(row["max_drawdown"]) for row in group),
            "mean_fill_rate": mean(float(row["fill_rate"]) for row in group),
        }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "stress_runs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.output_dir / "stress_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    write_experiment_manifest(
        args.output_dir / "experiment_manifest.json",
        experiment_name="latency_volatility_stress_sweep",
        execution_mode="intensity_fill_model",
        configuration={
            "volatilities": volatilities,
            "latencies": latencies,
            "paths": args.paths,
            "strategy": asdict(strategy_config),
            "adaptive_strategy": asdict(adaptive_config),
        },
        strategy_names=[
            "naive_symmetric",
            "inventory_skew",
            "avellaneda_stoikov",
            "microstructure_adaptive",
        ],
        seeds=list(range(args.paths)),
        repository_root=Path(__file__).resolve().parents[1],
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


def _key(strategy: str, volatility: float, latency: float) -> str:
    return f"{strategy}|vol={volatility:g}|latency={latency:g}"


if __name__ == "__main__":
    main()
