"""Statistical summaries for repeated simulation runs."""

from __future__ import annotations

from collections.abc import Iterable
from math import sqrt
from random import Random
from statistics import mean, stdev

from lob_sim.simulation import RunResult


def quantile(values: Iterable[float], probability: float) -> float:
    """Linearly interpolated sample quantile without a numerical dependency."""

    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a quantile of an empty sample")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must lie in [0, 1]")
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def summarize_runs(runs: Iterable[RunResult]) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[RunResult]] = {}
    for run in runs:
        grouped.setdefault(run.strategy_name, []).append(run)

    summary: dict[str, dict[str, float]] = {}
    for strategy, strategy_runs in grouped.items():
        final_pnls = [run.metrics["final_pnl"] for run in strategy_runs]
        mean_pnl = mean(final_pnls)
        sample_std = stdev(final_pnls) if len(final_pnls) > 1 else 0.0
        summary[strategy] = {
            "paths": float(len(strategy_runs)),
            "mean_final_pnl": mean_pnl,
            "std_final_pnl": sample_std,
            "standard_error": sample_std / sqrt(len(final_pnls)) if final_pnls else 0.0,
            "p05_final_pnl": quantile(final_pnls, 0.05),
            "p50_final_pnl": quantile(final_pnls, 0.50),
            "p95_final_pnl": quantile(final_pnls, 0.95),
            "mean_max_abs_inventory": mean(
                run.metrics["max_abs_inventory"] for run in strategy_runs
            ),
            "mean_drawdown": mean(run.metrics["max_drawdown"] for run in strategy_runs),
            "mean_fill_rate": mean(run.metrics["fill_rate"] for run in strategy_runs),
        }
        normalized = [normalized_run_metrics(run) for run in strategy_runs]
        for key in (
            "pnl_per_second",
            "pnl_per_traded_unit",
            "inventory_rms",
            "max_drawdown_pct",
            "filled_quantity_rate",
            "quote_to_trade_ratio",
        ):
            summary[strategy][f"mean_{key}"] = mean(item[key] for item in normalized)
        threshold = quantile(final_pnls, 0.05)
        tail = [value for value in final_pnls if value <= threshold]
        summary[strategy]["expected_shortfall_p05"] = mean(tail)
    return summary


def compare_runs(runs: Iterable[RunResult]) -> list[dict[str, float | str]]:
    """Return one flat row per run, convenient for CSV export."""

    rows: list[dict[str, float | str]] = []
    for run in runs:
        row: dict[str, float | str] = {
            "strategy": run.strategy_name,
            "seed": float(run.seed),
        }
        row.update(run.metrics)
        row.update(normalized_run_metrics(run))
        rows.append(row)
    return rows


def normalized_run_metrics(run: RunResult) -> dict[str, float]:
    """Return units-aware metrics derived from one completed run."""

    regular_fills = [fill for fill in run.fills if not fill.is_liquidation]
    traded_quantity = sum(fill.quantity for fill in regular_fills)
    quoted_quantity = sum(quote.bid_size + quote.ask_size for quote in run.quotes)
    duration = max(run.timestamps[-1] - run.timestamps[0], 1e-12)
    inventory_rms = sqrt(mean(value * value for value in run.inventory))
    final_pnl = run.metrics["final_pnl"]
    return {
        "total_traded_quantity": float(traded_quantity),
        "turnover_notional_ticks": sum(
            fill.price_ticks * fill.quantity for fill in regular_fills
        ),
        "pnl_per_second": final_pnl / duration,
        "pnl_per_traded_unit": final_pnl / traded_quantity if traded_quantity else 0.0,
        "inventory_rms": inventory_rms,
        "max_drawdown_pct": run.metrics["max_drawdown"]
        / max(run.wealth)
        if run.wealth
        else 0.0,
        "filled_quantity_rate": traded_quantity / quoted_quantity if quoted_quantity else 0.0,
        "quote_to_trade_ratio": (
            len(run.quotes) / len(regular_fills) if regular_fills else 0.0
        ),
    }


def paired_comparison(
    runs: Iterable[RunResult],
    *,
    baseline_strategy: str,
    candidate_strategy: str,
    bootstrap_samples: int = 2_000,
    seed: int = 17,
) -> dict[str, float]:
    """Compare two strategies with common-random-number paired differences."""

    grouped: dict[str, dict[int, float]] = {}
    for run in runs:
        grouped.setdefault(run.strategy_name, {})[run.seed] = run.metrics["final_pnl"]
    if baseline_strategy not in grouped or candidate_strategy not in grouped:
        raise ValueError("both strategies must be present")
    baseline = grouped[baseline_strategy]
    candidate = grouped[candidate_strategy]
    if set(baseline) != set(candidate):
        raise ValueError("paired strategies must share exactly the same seeds")
    deltas = [candidate[seed_value] - baseline[seed_value] for seed_value in sorted(baseline)]
    if not deltas:
        raise ValueError("at least one paired run is required")
    mean_delta = mean(deltas)
    sample_std = stdev(deltas) if len(deltas) > 1 else 0.0
    rng = Random(seed)
    bootstrap_means = []
    for _ in range(max(1, bootstrap_samples)):
        sample = [deltas[rng.randrange(len(deltas))] for _ in deltas]
        bootstrap_means.append(mean(sample))
    return {
        "pairs": float(len(deltas)),
        "mean_delta_pnl": mean_delta,
        "std_delta_pnl": sample_std,
        "standard_error": sample_std / sqrt(len(deltas)) if deltas else 0.0,
        "bootstrap_ci95_low": quantile(bootstrap_means, 0.025),
        "bootstrap_ci95_high": quantile(bootstrap_means, 0.975),
        "probability_candidate_beats": sum(delta > 0 for delta in deltas) / len(deltas),
    }


def paired_comparisons(
    runs: Iterable[RunResult],
    *,
    baseline_strategy: str,
    bootstrap_samples: int = 2_000,
) -> dict[str, dict[str, float]]:
    """Return paired PnL comparisons against one baseline strategy."""

    materialized = list(runs)
    strategies = sorted({run.strategy_name for run in materialized})
    return {
        strategy: paired_comparison(
            materialized,
            baseline_strategy=baseline_strategy,
            candidate_strategy=strategy,
            bootstrap_samples=bootstrap_samples,
        )
        for strategy in strategies
        if strategy != baseline_strategy
    }
