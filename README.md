# Adaptive Market Making Engine

This project builds a market-making agent inside a reproducible simulator. The
agent posts bids and asks, receives fills, manages inventory, estimates risk,
and is evaluated across controlled market regimes. The goal is research
quality and clear reasoning, not a claim of real-world profitability.

The mathematical and software contract is in
[docs/model_spec.md](docs/model_spec.md). The implementation boundaries are
summarized in [docs/architecture.md](docs/architecture.md), and the first
measured comparison is in [docs/technical_report.md](docs/technical_report.md).

## Market making from first principles

A market maker continuously offers two prices:

- The **bid** is the price at which the agent is willing to buy.
- The **ask** is the price at which the agent is willing to sell.

The difference between them is the **quoted spread**. If the agent buys at its
bid and later sells at its ask, it can earn the spread before fees and price
changes. A fill is not guaranteed: another participant must choose to trade
against the quote, and the order may be behind other orders in the queue.

The central risk is inventory. A bid fill increases inventory; an ask fill
decreases it. If the agent buys a lot and the reference price falls, the loss
on the remaining inventory can exceed the spread collected. Good quoting is
therefore a control problem under uncertainty:

1. quote far enough from the reference price to avoid low-quality fills;
2. quote close enough to obtain useful execution;
3. skew quotes when inventory is unbalanced;
4. widen or reduce size when volatility, latency, or adverse order flow rises;
5. stop or liquidate when hard risk limits are reached.

The engine compares four strategies on identical simulated markets:

1. naive symmetric quoting;
2. inventory-skewed quoting;
3. Avellaneda-Stoikov-style quoting.
4. microstructure-adaptive quoting using imbalance, signed flow, volatility,
   toxicity, and inventory-aware sizing.

The adaptive policy is deliberately interpretable: every quote records the
features that moved its reservation price, width, or size.

## What will be measured

Performance is not summarized by final PnL alone. Every run records spread
capture, inventory mark-to-market PnL, fees, adverse selection, fills,
turnover, drawdown, inventory exposure, and terminal liquidation cost.
Monte Carlo experiments report distributions and confidence intervals, with
common random seeds when strategies are compared.

## Current implementation

The repository now contains a dependency-light Python package under
`src/lob_sim` with:

- a deterministic price-time-priority limit-order book;
- integer-tick order and trade models;
- cash, inventory, wealth, and PnL attribution;
- causal event-time and clock-time volatility estimators;
- naive, inventory-skewed, Avellaneda-Stoikov-style, and microstructure-
  adaptive strategies;
- quote rounding, passive validation, reduced-risk mode, hard inventory limits,
  and kill switches;
- causal imbalance, signed trade flow, cancellation, queue-depletion, and
  toxicity features in the external LOB adapter;
- a seeded intensity-fill simulator with latency, queue decay, and JSON event tapes;
- a limit-order-book execution adapter using ordinary price-time matching;
- an optional adapter for the sibling Project 1 event-driven LOB, including
  FIFO queue position, partial fills, cancellations, and seeded order flow;
- a versioned persisted Project 1 event-tape format with deterministic replay;
- executable bid/ask inventory marking for external-book wealth;
- paired common-random-number comparisons, normalized execution metrics,
  experiment manifests, and dependency-free SVG comparison plots.

Run the checks with:

```bash
python -m pytest --cov=lob_sim --cov-report=term-missing
ruff check src tests scripts
```

Generate a comparison artifact with:

```bash
python scripts/run_comparison.py --paths 100 --output-dir artifacts
```

The output includes `comparison_summary.json`, `run_metrics.csv`, and
`representative_comparison.svg`.

Run the parameter-selection audit with disjoint development and holdout seeds:

```bash
python scripts/run_holdout_selection.py \
  --dev-paths 30 \
  --holdout-paths 30 \
  --output-dir artifacts/holdout
```

This tunes one interpretable parameter per non-naive strategy family using
common-random-number development paths, freezes the winners, and evaluates
them once on untouched holdout paths.

Run the latency/volatility stress matrix with:

```bash
python scripts/run_stress_sweep.py --paths 30 --output-dir artifacts/stress
```

This writes per-run CSV data and grouped JSON summaries for all four
strategies across three volatility levels and four latency levels.

Run the persistent-book comparison with:

```bash
python scripts/run_lob_comparison.py --paths 30 --output-dir artifacts/lob
```

The resulting trajectories and summary are separate from the intensity-model
comparison because the execution assumptions are different.

Run the four-strategy comparison through the standalone order-book
simulator with:

```bash
python scripts/run_external_comparison.py \
  --simulator-root ../Limit_Order_Book_Simulator \
  --paths 30 \
  --output-dir artifacts/external_lob
```

Add a causal toxic-flow treatment, where aggressive market buys move the next
reference point upward and sells move it downward:

```bash
python scripts/run_external_comparison.py \
  --simulator-root ../Limit_Order_Book_Simulator \
  --paths 100 \
  --toxic-response-ticks 4 \
  --output-dir artifacts/external_lob_100_toxic
```

This adapter does not copy or modify the Project 1 simulator. It loads its
`qr_platform` package at runtime, seeds it with the market path's initial
top-of-book, submits the agent's real limit orders, processes one external
event per interval, and converts the resulting trades into the engine's
accounting ledger. The synthetic reference path remains exogenous, while the
external book state and queue mechanics are endogenous to the event stream.
Agent orders are protected from background cancellation events because the
Project 1 generator does not distinguish participant ownership; market and
crossing-limit events can still fill them. Requoting is cancel-and-new and
therefore resets queue priority.

Create a persisted external event tape:

```bash
./.venv/bin/python scripts/make_external_event_tape.py \
  --simulator-root /Users/alessandrodellacqua/Downloads/Codex/HRT_application/Limit_Order_Book_Simulator \
  --seed 0 \
  --output artifacts/external_lob/event_tape_seed0.json
```

Replay that exact tape through all four strategies:

```bash
./.venv/bin/python scripts/run_external_replay.py \
  --simulator-root /Users/alessandrodellacqua/Downloads/Codex/HRT_application/Limit_Order_Book_Simulator \
  --event-tape artifacts/external_lob/event_tape_seed0.json \
  --output-dir artifacts/external_replay
```

The tape contains the exogenous reference path and one validated Project 1
event per interval. It is identified by a SHA-256 digest and can be replayed
across strategies without regenerating order flow. Each experiment also writes
`experiment_manifest.json` with resolved configuration, strategy parameters,
source-tree digests, external-simulator digest, seeds, and input-tape digests.

Measure the current correctness-first baseline with:

```bash
python scripts/benchmark.py --orders 20000 --paths 20 --output artifacts/benchmark.json
```

## Project status

The initial implementation milestones are complete. Remaining work is
execution realism and deeper research experiments:

- [x] accounting and state representation;
- [x] deterministic event-driven intensity simulator;
- [x] naive and inventory-aware strategies;
- [x] volatility, fills, and Avellaneda-Stoikov quoting;
- [x] experiments, plots, paired comparisons, manifests, and technical report;
- [x] order-book integration, latency, and queue effects;
- [x] stress matrix;
- [x] measured performance benchmark;
- [x] external Project 1 LOB adapter and event-driven strategy comparison.
- [x] persisted external event-tape generation and replay.
- [x] causal toxic-flow treatment and executable bid/ask inventory marking.

## Reproducibility principles

- Every experiment has an explicit configuration and seed.
- Strategy comparisons reuse the same market paths where possible.
- The event tape, actions, fills, and risk decisions are logged.
- Source and external-simulator digests are stored with experiment manifests.
- A fixed seed must reproduce the same actions, fills, and PnL.
- The simulator never gives a strategy access to future prices or future
  volatility observations.
