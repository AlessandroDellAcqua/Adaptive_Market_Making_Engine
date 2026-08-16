"""Measure the current correctness-first simulator baseline."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from lob_sim.agents import NaiveSymmetricMarketMaker, StrategyConfig
from lob_sim.core.book import LimitOrderBook
from lob_sim.core.models import OrderRequest, Side
from lob_sim.simulation import IntensityMarketSimulator, SimulationConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=20_000)
    parser.add_argument("--paths", type=int, default=20)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.orders <= 0 or args.paths <= 0:
        raise SystemExit("--orders and --paths must be positive")

    book_result = benchmark_book(args.orders)
    simulation_result = benchmark_simulation(args.paths)
    result = {"book": book_result, "simulation": simulation_result}
    serialized = json.dumps(result, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


def benchmark_book(order_count: int) -> dict[str, float]:
    book = LimitOrderBook()
    order_ids: list[int] = []
    start = time.perf_counter()
    for index in range(order_count):
        if index % 2 == 0:
            request = OrderRequest(
                client_order_id=f"bench-bid-{index}",
                side=Side.BUY,
                quantity=1,
                price_ticks=10_000 - index % 100,
                owner="benchmark",
            )
        else:
            request = OrderRequest(
                client_order_id=f"bench-ask-{index}",
                side=Side.SELL,
                quantity=1,
                price_ticks=10_200 + index % 100,
                owner="benchmark",
            )
        order_ids.append(book.submit(request).order_id)
    elapsed = time.perf_counter() - start
    for order_id in order_ids[::2]:
        book.cancel(order_id)
    book.assert_invariants()
    return {
        "submitted_orders": float(order_count),
        "insert_seconds": elapsed,
        "insert_orders_per_second": order_count / elapsed,
        "active_orders_after_cancels": float(book.active_order_count),
    }


def benchmark_simulation(paths: int) -> dict[str, float]:
    config = SimulationConfig(horizon_seconds=10.0, dt_seconds=0.1)
    simulator = IntensityMarketSimulator(config)
    strategy = NaiveSymmetricMarketMaker(StrategyConfig())
    start = time.perf_counter()
    runs = [simulator.run(strategy, seed=seed) for seed in range(paths)]
    elapsed = time.perf_counter() - start
    intervals = sum(len(run.quotes) for run in runs)
    return {
        "paths": float(paths),
        "simulation_seconds": elapsed,
        "intervals_per_second": intervals / elapsed,
        "mean_final_pnl": sum(run.metrics["final_pnl"] for run in runs) / paths,
    }


if __name__ == "__main__":
    main()
