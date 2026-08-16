# Technical Report: Adaptive Market Making Engine

Status: reproducible research comparison with synthetic, persistent-L2, and
Project 1 event-driven execution modes.

## Executive summary

This project implements a single-instrument market-making research engine. It
quotes two-sided prices, receives stochastic or order-book fills, tracks cash
and inventory, marks wealth, applies risk controls, and compares four policies:

1. naive symmetric quoting;
2. linear inventory-skewed quoting;
3. finite-horizon Avellaneda-Stoikov-style quoting;
4. an interpretable microstructure-adaptive policy.

The central result is a trade-off, not a universal ranking. In the 100-path
Project 1 LOB experiment, Avellaneda-Stoikov had the lowest inventory and the
best mean PnL under the balanced synthetic flow. In the 100-path toxic-flow
treatment, its mean PnL increased to `0.222` currency units and it beat the
naive baseline on 76% of paired paths. The adaptive policy reduced exposure
relative to naive quoting, but its paired advantage in balanced flow was not
statistically decisive. That is the appropriate conclusion for an
uncalibrated simulator: the mechanisms are visible and testable, but the
numbers are not a claim of live-market profitability.

## 1. Market-making model

A market maker posts a bid, the price at which it buys, and an ask, the price
at which it sells. Their difference is the quoted spread. A fill is an
execution against the quote, not a guaranteed trade: the order can remain
unfilled or sit behind other orders in a FIFO queue.

Inventory is signed. A bid fill increases inventory and an ask fill decreases
it. If the maker accumulates a long position and the reference price falls,
inventory mark-to-market losses can exceed the spread collected. The engine
therefore treats quoting as a control problem with four linked decisions:

- where to place the reservation price;
- how wide to quote around it;
- how much size to expose on each side;
- when to reduce, halt, or liquidate risk.

The controlled reference process is an exogenous Gaussian price path. In the
external LOB mode, the reference path remains exogenous while the agent's
orders participate in Project 1's matching engine. This isolates execution and
inventory effects without pretending to model market impact.

## 2. State, features, and strategy equations

The observation contains top-of-book bid/ask prices and sizes, midprice,
microprice, spread, causal volatility, and—when available—signed trade flow,
cancellation rate, queue depletion, and a bounded toxicity score.

For top-level sizes `V_b` and `V_a`:

```text
imbalance I_t = (V_b - V_a) / (V_b + V_a)
microprice   = (ask_t V_b + bid_t V_a) / (V_b + V_a)
```

The intensity execution model uses:

```text
lambda(delta) = A exp(-k delta)
P(fill in dt) = 1 - exp(-lambda(delta) dt)
```

The four strategies are:

| Strategy | Reservation price | Width / size response |
| --- | --- | --- |
| Naive symmetric | reference | fixed half-width, fixed size |
| Inventory skew | `S - kappa_q q_scaled` | fixed half-width |
| Avellaneda-Stoikov | `S - q gamma sigma^2 tau` | volatility, horizon, and fill-decay terms |
| Microstructure adaptive | `S + kappa_I I + kappa_F F - kappa_q q_scaled` | widens with volatility/toxicity and reduces size |

The adaptive policy is deliberately interpretable. It only uses features
available at the decision timestamp. In the external adapter, aggressive-flow
features are computed from cumulative Project 1 trade and cancellation
counters; no future price is passed into the quote decision.

## 3. Accounting and risk controls

The ledger is the source of truth for cash, inventory, wealth, and PnL
attribution. The additive identity is tested event by event:

```text
wealth_T - wealth_0
  = spread_capture
    + inventory_mark_to_market
    - fees
    + liquidation_pnl
    + executable_mark_adjustment
```

Adverse selection is retained as a diagnostic and is not subtracted twice from
the identity. In external-book mode, inventory is marked at the executable
best bid when long and executable best ask when short. The synthetic reference
is still recorded for fill-edge and post-fill adverse-selection analysis.

The shared risk layer has three active control states plus terminal handling:

| State | Behaviour |
| --- | --- |
| `NORMAL` | Quote permitted sides at configured size |
| `REDUCED` | Widen quotes and cap size as inventory approaches `q_max` |
| `HALTED` | Submit no new risk-increasing passive quotes |
| liquidation | Flatten residual inventory at the terminal policy |

Live-order quantities are included in worst-case inventory checks, so a quote
cannot evade `q_max` merely because several resting orders have not filled yet.

## 4. Experiment design

The primary configuration is:

- one instrument, integer ticks, tick value `0.01`;
- initial reference `10,000` ticks;
- 20-second horizon with 0.1-second decisions;
- volatility `5` ticks per square-root-second;
- passive fee `0.005` per unit;
- inventory target `0`, hard limit `10`;
- terminal flattening with two ticks of deterministic slippage.

Strategies share common market paths and execution uniforms wherever the
execution mode permits. This is a common-random-number design: it reduces the
variance of strategy differences while preserving the fact that different
quotes receive different fills.

The analysis reports individual distributions and paired differences. Paired
comparisons use candidate-minus-naive PnL, a deterministic bootstrap of paired
differences, and the fraction of paths where the candidate beats naive. The
run-level CSV additionally records PnL per second, PnL per traded unit,
inventory RMS, filled-quantity rate, quote-to-trade ratio, and 5% expected
shortfall.

The selection audit in `artifacts/holdout/` uses seeds `0–29` for development
and disjoint seeds `30–59` for holdout. It selects one interpretable parameter
per non-naive family using `mean PnL - 0.05 * mean inventory RMS`, then freezes
those parameters before the holdout run. This audit is separate from the
headline table below, whose parameters are fixed configuration examples.

## 5. Results: intensity model

The 100-path run is saved in `artifacts/comparison_summary.json` and uses the
same exogenous path and intensity-fill configuration for all four policies.

| Strategy | Mean PnL | Std. dev. | Approx. 95% CI | P05 | P95 | Max inventory | Drawdown | Fill rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Naive symmetric | 0.362 | 0.748 | [0.215, 0.508] | -0.915 | 1.451 | 5.76 | 0.676 | 13.91% |
| Inventory skew | 0.398 | 0.393 | [0.321, 0.475] | -0.135 | 1.123 | 3.62 | 0.334 | 12.07% |
| Avellaneda-Stoikov | 0.063 | 0.150 | [0.033, 0.092] | -0.141 | 0.340 | 1.02 | 0.101 | 0.94% |
| Microstructure adaptive | 0.401 | 0.414 | [0.320, 0.482] | -0.321 | 1.071 | 3.41 | 0.345 | 8.25% |

Paired PnL differences versus naive were:

| Candidate | Mean delta | Bootstrap 95% CI | Candidate beats naive |
| --- | ---: | ---: | ---: |
| Inventory skew | +0.037 | [-0.059, +0.135] | 54% |
| Avellaneda-Stoikov | -0.299 | [-0.438, -0.164] | 27% |
| Microstructure adaptive | +0.039 | [-0.071, +0.147] | 51% |

The intensity result shows the expected control trade-off: naive quoting has
the highest fill rate but largest inventory and drawdown; Avellaneda-Stoikov
is very conservative; the adaptive policy trades less than naive and reaches
lower maximum inventory. The separate development/holdout audit below tests
whether a simple parameter-selection rule survives on untouched paths.

### Development/holdout selection audit

The development winners were `inventory_kappa=0.5`, `gamma=0.05`, and
`imbalance_kappa=1.5` for the inventory, Avellaneda-Stoikov, and adaptive
families respectively. On the untouched 30-path holdout:

| Strategy | Mean PnL | Mean inventory RMS | PnL delta vs naive | Bootstrap 95% CI | Beats naive |
| --- | ---: | ---: | ---: | ---: | ---: |
| Naive fixed | 0.255 | 3.29 | — | — | — |
| Inventory skew | 0.334 | 1.87 | +0.079 | [-0.121, +0.287] | 53% |
| Avellaneda-Stoikov | 0.171 | 0.98 | -0.084 | [-0.343, +0.183] | 37% |
| Microstructure adaptive | 0.339 | 1.81 | +0.084 | [-0.143, +0.301] | 53% |

The intervals are wide at 30 holdout paths, which is useful evidence rather
than a weakness to hide: selecting on development data does not make the
holdout comparison look artificially precise.

## 6. Results: persistent synthetic L2

The persistent-book experiment uses queue-aware price-time matching, delayed
quote replacement, partial fills, and background replenishment. Its
parameters differ from the primary experiment, so it is an execution-layer
check rather than a second profitability ranking.

| Strategy | Mean PnL | Max inventory | Drawdown | Fill rate |
| --- | ---: | ---: | ---: | ---: |
| Naive symmetric | -0.046 | 3.67 | 0.613 | 5.08% |
| Inventory skew | -0.052 | 3.50 | 0.567 | 5.00% |
| Avellaneda-Stoikov | 0.330 | 2.57 | 0.297 | 1.67% |
| Microstructure adaptive | 0.132 | 2.87 | 0.454 | 3.78% |

The output is in `artifacts/lob/lob_comparison_summary.json`.

## 7. Results: Project 1 event-driven LOB

The optional external adapter loads the sibling `Limit_Order_Book_Simulator`
at runtime. Project 1 is the source of truth for FIFO queueing, limit and
market events, cancellations, partial fills, and multi-level execution. The
adapter leaves that repository unchanged.

### Balanced synthetic flow

The 100-path balanced-flow run is in `artifacts/external_lob_100_adaptive/`.
All strategies use the same event-flow seeds. The reference price is
exogenous, but live agent orders can change their own book state and queue
position.

| Strategy | Mean PnL | Std. dev. | Approx. 95% CI | P05 | P95 | Max inventory | Drawdown | Fill rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Naive symmetric | -0.055 | 0.283 | [-0.111, 0.001] | -0.553 | 0.350 | 4.31 | 0.409 | 5.92% |
| Inventory skew | -0.128 | 0.363 | [-0.199, -0.057] | -0.663 | 0.074 | 3.76 | 0.362 | 5.93% |
| Avellaneda-Stoikov | 0.030 | 0.081 | [0.014, 0.046] | -0.070 | 0.190 | 0.76 | 0.108 | 0.60% |
| Microstructure adaptive | -0.013 | 0.126 | [-0.038, 0.012] | -0.193 | 0.201 | 2.80 | 0.318 | 3.93% |

The paired comparison versus naive is more informative than ranking the
individual means:

| Candidate | Mean delta | Bootstrap 95% CI | Candidate beats naive |
| --- | ---: | ---: | ---: |
| Inventory skew | -0.073 | [-0.167, +0.017] | 36% |
| Avellaneda-Stoikov | +0.085 | [+0.027, +0.143] | 66% |
| Microstructure adaptive | +0.042 | [-0.019, +0.106] | 51% |

The external adapter reports executable-mark adjustment separately. This
prevents an inventory position from being valued at an unattainable midprice
when computing wealth.

### Causal toxic-flow treatment

The treated run is in `artifacts/external_lob_100_toxic/` and adds a four-tick
next-step response: an aggressive market buy moves the next reference point up,
and a market sell moves it down, while the original Gaussian increment is
retained. A dedicated acceptance test verifies that the conditional next move
is higher after buys than sells.

| Strategy | Mean PnL | Max inventory | Drawdown | Fill rate | P05 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Naive symmetric | -0.024 | 3.98 | 0.280 | 10.00% | -0.241 |
| Inventory skew | -0.014 | 2.47 | 0.230 | 9.05% | -0.131 |
| Avellaneda-Stoikov | 0.222 | 1.05 | 0.266 | 0.90% | 0.000 |
| Microstructure adaptive | 0.005 | 2.72 | 0.221 | 4.99% | -0.171 |

Against naive, Avellaneda-Stoikov had mean paired improvement `+0.246`, a
bootstrap interval `[+0.185, +0.314]`, and won 76% of paths. Inventory skew
was statistically indistinguishable from naive at this configuration, while
the adaptive policy's paired improvement was `+0.029` with interval
`[+0.010, +0.052]` and 52% wins. This is a causal stress result, not a
calibration claim.

## 8. Persisted replay and provenance

The seed-0 persisted tape has digest
`1ba2142cf727b9798ed41d6a938ea74c2bc54162da956d0d31f8c0790ace625a`.
It replays byte-for-byte through all four strategies. The audit results are:

| Strategy | Final PnL | Max inventory | Fill rate |
| --- | ---: | ---: | ---: |
| Naive symmetric | -0.060 | 5 | 16.5% |
| Inventory skew | 0.070 | 2 | 14.5% |
| Avellaneda-Stoikov | 0.000 | 0 | 0.0% |
| Microstructure adaptive | -0.060 | 3 | 7.5% |

Each experiment writes a versioned manifest with resolved configuration,
strategy parameters, seeds, Python/platform information, a path-independent
repository digest, the external-simulator digest when used, and input-tape
digests. Generated artifacts are excluded from source hashing so output files
do not contaminate code identity.

## 9. Validation evidence

The final suite has 33 passing tests and 88% statement coverage for
`lob_sim`. It includes:

- hand-computed cash, inventory, executable-mark, and PnL closure tests;
- property tests for non-crossing quotes, queue monotonicity, and inventory
  limits;
- causal volatility and toxic-flow direction tests;
- adaptive quote response and reduced-risk-state tests;
- paired-statistics and normalized-metric tests;
- deterministic intensity, persistent-L2, and Project 1 replay tests;
- corrupted external-tape schema rejection;
- source-digest and manifest tests;
- representative SVG plots, CSV run tables, and saved JSON summaries.

Run the checks with:

```bash
./.venv/bin/pytest -q --cov=lob_sim --cov-report=term-missing
./.venv/bin/ruff check .
```

## 10. Limitations and next research steps

The most important limitations are explicit:

- the reference price and external flow are synthetic, not historically
  calibrated;
- no market impact or participant ownership model is claimed;
- new-order latency exists, but cancel latency is not separately parameterized;
- fees/rebates are simplified and exchange-specific self-trade prevention is
  not modelled;
- the benchmark is correctness-first and does not claim production throughput,
  memory, or tail-latency performance;
- the synthetic intensity mode now has a frozen development/holdout selector,
  but external-LOB coefficients are still manually specified.

The next credible research step is to introduce a documented historical event
tape with a source license, commit/hash, and calibration notebook. The tuning
protocol should extend the development/holdout split to external-LOB widths,
inventory penalties, toxicity coefficients, and `gamma`, then report an
efficient frontier over PnL, inventory RMS, expected shortfall, fill rate, and
quote-to-trade ratio.
