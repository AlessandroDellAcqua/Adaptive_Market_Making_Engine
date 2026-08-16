"""Development-set selection helpers for frozen strategy experiments."""

from __future__ import annotations

from collections.abc import Iterable
from statistics import mean

from lob_sim.analysis.metrics import normalized_run_metrics
from lob_sim.simulation import RunResult


def rank_candidates(
    candidates: dict[str, Iterable[RunResult]],
    *,
    inventory_penalty: float = 0.05,
) -> list[dict[str, float | str]]:
    """Rank candidate parameterizations on a development set.

    The score is intentionally simple and auditable: mean terminal PnL minus a
    configurable penalty on mean inventory RMS. The holdout set must not be
    passed to this function.
    """

    if inventory_penalty < 0:
        raise ValueError("inventory_penalty must be non-negative")
    ranked: list[dict[str, float | str]] = []
    for name, runs in candidates.items():
        materialized = list(runs)
        if not materialized:
            raise ValueError(f"candidate {name!r} has no development runs")
        mean_pnl = mean(run.metrics["final_pnl"] for run in materialized)
        mean_inventory_rms = mean(
            normalized_run_metrics(run)["inventory_rms"] for run in materialized
        )
        mean_drawdown = mean(run.metrics["max_drawdown"] for run in materialized)
        ranked.append(
            {
                "candidate": name,
                "selection_score": mean_pnl - inventory_penalty * mean_inventory_rms,
                "mean_final_pnl": mean_pnl,
                "mean_inventory_rms": mean_inventory_rms,
                "mean_drawdown": mean_drawdown,
                "paths": float(len(materialized)),
            }
        )
    return sorted(ranked, key=lambda row: float(row["selection_score"]), reverse=True)
