# Adaptive Market Making Engine: Model Specification

Status: implemented research specification; historical calibration and market
impact remain outside the current scope

This specification converts the project manual into a precise first version
of the model. It deliberately starts with a small, testable system and adds
realism one controlled feature at a time.

## 1. Objective and guiding idea

The engine controls a single market-making agent for one instrument over a
finite horizon `[0, T]`. At each decision time it observes the market and its
own account, then submits, replaces, or cancels passive limit orders.

The agent is evaluated on the joint distribution of PnL and risk, not on one
lucky path. The core evaluation objective is:

```text
J = E[PnL_T]
    - alpha * Var(PnL_T)
    - beta  * E[sum_t q_t^2 * dt]
    - eta   * E[max_drawdown]
    - zeta  * E[transaction_costs]
```

This is an evaluation score, not necessarily the exact objective optimized by
every strategy. Reporting the individual terms prevents a high PnL strategy
from hiding unacceptable inventory or drawdown risk.

The implementation separates strategy quote intents, shared risk sanitization,
execution adapters, and normalized fills. This keeps accounting and risk
invariants independent of any one matching engine.

## 2. Market making from first principles

### 2.1 Quotes create a two-sided offer

Let `S_t` be the reference price. A symmetric quote with half-width `delta`
is:

```text
bid_t = S_t - delta
ask_t = S_t + delta
```

The quoted spread is `ask_t - bid_t = 2 * delta`. A buy fill at the bid is
favorable relative to the reference at that instant; a sell fill at the ask is
also favorable. For a round trip of equal size, the idealized gross edge is
approximately the quoted spread.

That edge is compensation for providing immediacy and taking risk. The agent
does not know when it will be filled, whether only one side will fill, or how
the reference price will move while inventory is held.

### 2.2 Fills create inventory risk

Inventory `q_t` is positive when the agent owns the instrument and negative
when it is short. A bid fill increases inventory; an ask fill decreases it.
If the agent buys at price `p` and the reference later moves from `S` to
`S + dS`, its inventory contributes approximately `q * dS` to wealth change.
This is the main reason a strategy can collect many small spreads and still
lose money.

### 2.3 Order flow affects both fills and price direction

A fill is more likely when other participants want to trade at the agent's
price. That demand can be informative: one-sided aggressive buying may make an
ask fill likely immediately before the reference price rises. This is
**adverse selection**. The simulator therefore records both the fill and the
subsequent price move rather than treating all fills as equally good.

## 3. Time and units

- The simulator is event-driven. Events have nondecreasing timestamps `t` in
  seconds and a finite terminal time `T`.
- Decision intervals are allowed to vary. When a fixed grid is useful, use
  `dt = t_(n+1) - t_n`.
- Prices are represented as integer ticks for order submission. A reporting
  price is `price = price_ticks * tick_size`.
- Quantities are integer contracts or shares unless the instrument config says
  otherwise.
- Volatility is estimated in price units per square-root-second for quote
  widths. If the estimator uses log returns, its output is log-volatility;
  convert it to price volatility with `sigma_price = S_t * sigma_log` before
  using the Avellaneda-Stoikov quote-width equations.
- `cash` and PnL are in currency units. All accounting uses the configured
  tick value and fee model.

Using integer price ticks is important: a mathematically valid continuous
quote must still be a valid exchange price after rounding.

## 4. State variables

### 4.1 Market observation

At decision time `t`, the agent receives an immutable `MarketObservation`:

| Field | Meaning |
| --- | --- |
| `timestamp` | Current simulator time `t` |
| `best_bid`, `best_ask` | Current market top of book in ticks |
| `bid_size`, `ask_size` | Visible top-level sizes |
| `midprice` | `(best_bid + best_ask) / 2` |
| `market_spread` | `best_ask - best_bid` |
| `imbalance` | `(bid_size - ask_size) / (bid_size + ask_size)` |
| `microprice` | `(best_ask*bid_size + best_bid*ask_size) / (bid_size + ask_size)` |
| `last_trade_price` | Most recent trade price, if available |
| `last_trade_sign` | Buyer- or seller-initiated sign, if available |
| `order_flow_features` | Recent signed flow and cancellation features |
| `volatility` | Volatility estimate using data available up to `t` |
| `session_end` | Whether the terminal liquidation window has started |

The first version uses `midprice` as the default reference. `microprice` is an
optional reference that shifts toward the side with less visible depth and is
tested as a separate experiment.

### 4.2 Agent state

`AgentState` contains the variables that belong to the strategy and account:

| Field | Meaning |
| --- | --- |
| `cash = X_t` | Cash account after fills and fees |
| `inventory = q_t` | Current signed inventory |
| `wealth = W_t` | Derived mark-to-market wealth `X_t + q_t*S_t` |
| `peak_wealth` | Highest observed wealth for drawdown |
| `drawdown` | `peak_wealth - wealth` |
| `active_orders` | Live orders and remaining quantities |
| `sigma` | Volatility estimator state |
| `risk_status` | Normal, reduced, halted, or liquidation |
| `pnl_buckets` | Spread, inventory, fees, adverse selection, and liquidation PnL |
| `quote_log` | Quote decisions and later execution outcomes |

`cash` and `inventory` are the accounting source of truth. `wealth` is always
recomputed from them and the current reporting reference price so stale derived
values cannot silently corrupt PnL.

### 4.3 Strategy and risk parameters

The common configuration includes:

```text
tick_size, tick_value
q_target, q_max
max_order_size
min_half_width, max_half_width
fee_per_unit, fee_rate
gamma, k, A
volatility_window, volatility_decay
latency, order_lifetime
max_drawdown, max_daily_loss
liquidation_horizon
reference_mode = midprice | microprice | fair_price
fill_mode = limit_order_book | intensity
```

For models using raw inventory in the reservation-price equation, `gamma` has
units that depend on the instrument, price units, and horizon. To make
experiments comparable across configurations, the implementation may also use
the normalized inventory:

```text
q_scaled = (q_t - q_target) / q_scale
```

where `q_scale` is documented in the experiment config. The same convention
must be used when comparing values of `gamma`.

## 5. Reference price and order flow

### 5.1 Midprice

For a valid two-sided top of book:

```text
m_t = (best_bid_t + best_ask_t) / 2
```

This is the neutral baseline. It is observable and easy to interpret, but it
does not use depth information.

### 5.2 Imbalance and microprice

With top-level bid and ask sizes `V_b` and `V_a`:

```text
I_t = (V_b - V_a) / (V_b + V_a)

microprice_t = (best_ask_t * V_b + best_bid_t * V_a) / (V_b + V_a)
```

`I_t` lies in `[-1, 1]` when the denominator is positive. A large positive
imbalance means more visible bid depth and moves the microprice toward the ask;
it is interpreted as upward pressure, not as a guaranteed prediction.

### 5.3 Optional fair-price process for controlled tests

Before integrating a full order book, a simple exogenous reference process can
provide deterministic unit tests and regime experiments:

```text
log(F_(t+dt) / F_t) = mu_regime * dt
                         + sigma_regime * sqrt(dt) * epsilon_t
epsilon_t ~ Normal(0, 1)
```

The simulator can construct a synthetic top of book around `F_t`, or use `F_t`
directly as the reference. The price path is exogenous in the baseline: the
agent does not move the market. Market impact is deliberately deferred until
the basic accounting and strategy comparisons are trusted.

Regimes are explicit configuration objects, for example:

- low volatility / tight spread / balanced flow;
- high volatility / tight spread / balanced flow;
- low volatility / wide spread / balanced flow;
- high volatility / wide spread / one-sided toxic flow.

## 6. Fill and execution model

### 6.1 Intensity model

For a quote at distance `delta >= 0` from the chosen reference price, the
first controlled fill model is:

```text
lambda(delta) = A * exp(-k * delta)
P(fill during dt) = 1 - exp(-lambda(delta) * dt)
```

`A` is the arrival intensity at zero distance and `k` controls how quickly
execution probability decays as a quote moves away. Bid and ask can have
different parameters when the regime has directional flow:

```text
lambda_b = A_b * exp(-k_b * delta_b)
lambda_a = A_a * exp(-k_a * delta_a)
```

The implementation must use the exponential probability formula, not simply
`lambda * dt`, except as a documented small-`dt` approximation. If partial
fills are enabled, the realized quantity is capped by the remaining order
quantity and the available simulated flow.

### 6.2 Queue-aware limit-order-book mode

When connected to the Project 1 order book, the matching engine is the source
of truth. The agent submits normal limit orders; fills arise from the normal
matching rules. The intensity model is then used only for controlled tests or
as a calibration diagnostic.

At minimum, every fill records:

```text
order_id, fill_id, timestamp, side, price_ticks, quantity,
queue_ahead_before, fee, reference_price_at_fill
```

Queue position must affect passive execution probability. A first practical
approximation is a monotone queue factor such as:

```text
lambda_effective = lambda(delta) * exp(-rho * queue_ahead / depth_scale)
```

The exact queue model is an experiment parameter; the monotonicity test is
not optional.

### 6.3 Latency

An action decided at time `t` arrives at the exchange at `t + latency`. During
that interval the quote is not active. Cancels and replacements follow the
same latency convention. The event tape must record both decision time and
arrival time so the experiment can distinguish stale quotes from bad strategy
logic.

## 7. Quoting strategies

All strategies produce desired continuous prices first. A common quote
sanitizer then rounds prices to ticks, checks market constraints, applies risk
gates, and creates the actual order action.

### 7.1 Naive symmetric market maker

Use a fixed half-width `h`:

```text
r_t = S_t
delta_b = delta_a = h
bid = r_t - h
ask = r_t + h
```

This is a deliberately weak baseline. It makes the inventory problem visible
because it does not react to a long or short position.

### 7.2 Inventory-skewed market maker

Shift the reservation price linearly toward the side that reduces inventory:

```text
r_t = S_t - kappa_q * q_scaled
delta_b = delta_a = h
bid = r_t - h
ask = r_t + h
```

If the agent is long (`q_scaled > 0`), both quotes move down. The ask becomes
closer and more attractive, while the bid becomes farther away. If the agent
is short, the opposite happens. The skew should be smooth except when a hard
risk limit removes a side entirely.

### 7.3 Avellaneda-Stoikov-style market maker

For remaining horizon `tau = max(T - t, 0)`, use the finite-horizon reference
formula from the manual:

```text
r_t = S_t - q_t * gamma * sigma_t^2 * tau

delta_t = gamma * sigma_t^2 * tau / 2
           + (1 / gamma) * log(1 + gamma / k)

bid_t = r_t - delta_t
ask_t = r_t + delta_t
```

The inventory term moves the reservation price against the current position.
The volatility and horizon terms widen the quote because holding inventory is
more dangerous when future price uncertainty is larger. The logarithmic term
captures the execution-vs-distance trade-off under exponential fill intensity.

Numerical safeguards:

- If `gamma` is zero, use the continuous limit of the logarithmic term rather
  than dividing by zero.
- Clamp `sigma` to a configured positive floor.
- Use `tau = 0` during terminal liquidation; do not quote normally once the
  liquidation policy is active.
- Cap the resulting half-width at `max_half_width` before rounding.
- Normalize inventory if the experiment config chooses `q_scaled`.

### 7.4 Optional reference-price variants

For a microprice-aware strategy, use:

```text
S_t = midprice_t + kappa_I * (microprice_t - midprice_t)
```

with a documented clamp so a noisy depth snapshot cannot move the reference
without bound. This variant is compared against the same quoting model using
midprice, not evaluated as a replacement for the required baseline strategies.

### 7.5 Microstructure-adaptive market maker

The implemented adaptive policy uses only features available at the current
decision time:

```text
r_t = S_t
      + kappa_I * I_t
      + kappa_F * F_t
      - kappa_q * q_scaled

delta_t = h
           + kappa_sigma * max(sigma_t - sigma_floor, 0)
           + kappa_T * toxicity_t
```

`I_t` is top-of-book imbalance, `F_t` is normalized signed trade flow, and
`toxicity_t` is bounded using signed flow, traded size, and visible depth.
Requested bid and ask sizes are reduced by toxicity and inventory before the
shared risk layer applies hard limits. Cancellation and queue-depletion rates
are computed and logged by the external adapter; they are deliberately not
given free predictive coefficients without calibration.

## 8. Quote sanitization and risk gates

After a strategy computes desired quotes:

1. Round bids down and asks up to valid integer ticks.
2. Enforce `bid < ask` whenever both sides are present.
3. Enforce passive non-crossing constraints against the current market.
4. Enforce minimum and maximum half-widths.
5. Cap each order at `max_order_size`.
6. Apply inventory and account risk gates.
7. Emit explicit cancel/replace actions for old order ids.

The inventory gate must account for live orders, not only current inventory. A
conservative worst-case check is:

```text
worst_case_inventory_after_all_live_bids = q_t + live_bid_remaining
worst_case_inventory_after_all_live_asks = q_t - live_ask_remaining
```

Do not submit or keep a bid if the first value would exceed `q_max`. Do not
submit or keep an ask if the second value would fall below `-q_max`. This
prevents an agent from exceeding the limit because several already-submitted
orders fill before a cancel reaches the exchange.

Risk states:

| State | Behaviour |
| --- | --- |
| `NORMAL` | Quote both permitted sides at configured size |
| `REDUCED` | Inventory is near the configured fraction of `q_max`; widen quotes and cap size |
| `HALTED` | Cancel passive orders and submit no new risk-increasing orders |
| `LIQUIDATION` | Execute the configured terminal inventory policy |

Triggers include maximum inventory, maximum drawdown or daily loss, invalid
market data, abnormal market spread, volatility above a hard threshold, and
the end-of-run liquidation window.

## 9. Accounting and PnL attribution

Let a fill have price `p`, quantity `u`, fee `f >= 0`, and reference price
`S_fill` at the fill timestamp.

Bid fill (the agent buys):

```text
q <- q + u
X <- X - p*u - f
spread_edge <- (S_fill - p) * u
```

Ask fill (the agent sells):

```text
q <- q - u
X <- X + p*u - f
spread_edge <- (p - S_fill) * u
```

At every mark in synthetic and intensity modes:

```text
W_t = X_t + q_t * S_t
```

The event log maintains the following attribution buckets:

- **spread capture:** the fill edge relative to the reference at execution;
- **inventory mark-to-market:** PnL from holding inventory while the reference
  price moves;
- **fees and transaction costs:** explicit charges, rebates, and liquidation
  costs;
- **adverse selection:** the post-fill move against the side of the trade over
  a configured horizon;
- **terminal liquidation:** cost or benefit of flattening residual inventory.

For an atomic event timeline, inventory PnL is accumulated between fills as
`q_held * (S_next - S_previous)`. The following identity must hold up to the
configured numerical tolerance:

```text
W_T - W_0
  = spread_capture
    + inventory_mark_to_market
    - fees_and_costs
    + liquidation_adjustment
```

In external LOB mode, wealth uses the executable bid for long inventory and
the executable ask for short inventory. The synthetic reference remains on
the fill for spread and adverse-selection diagnostics. The difference is
reported as `executable_mark_adjustment` so it cannot be mistaken for spread
capture.

This identity is tested before any strategy performance claim is made.

## 10. Volatility and regime estimation

The estimator may use log returns:

```text
r_n = log(S_n / S_(n-1))
```

### Event-time estimator

Update at each market event using elapsed time `dt_n`:

```text
instantaneous_variance_rate = r_n^2 / dt_n
w_n = 1 - exp(-dt_n / tau_vol)
v_n = (1 - w_n) * v_(n-1) + w_n * instantaneous_variance_rate
sigma_n = sqrt(max(v_n, sigma_floor^2))
```

### Clock-time estimator

Sample the reference on a regular grid, carry forward the latest available
price when no event occurs, then apply the same EWMA to grid returns. This
estimator measures a different object from event-time volatility and must not
be mixed silently in a comparison.

Both estimators are strictly causal: the quote at `t_n` can use data through
`t_n`, never the return ending after `t_n`. Startup behaviour, missing prices,
and zero elapsed time are explicit test cases.

## 11. Simulator interface

The engine should depend on a small adapter rather than on one particular
matching implementation. The following is a language-neutral contract that
will map naturally to Python protocols or dataclasses.

### Inputs and outputs

```text
OrderIntent:
    order_id
    side = BUY | SELL
    price_ticks
    quantity
    decision_time
    expires_at (optional)
    client_metadata

QuoteAction:
    decision_time
    cancel_order_ids[]
    new_orders[]: OrderIntent
    strategy_name
    model_inputs_snapshot

Fill:
    fill_id
    order_id
    timestamp
    side
    price_ticks
    quantity
    fee
    queue_ahead_before (optional)
    reference_price_at_fill

MarketObservation:
    timestamp
    order_book_snapshot
    reference_prices
    volatility_state
    order_flow_state
    session_end

StepResult:
    observation
    market_events[]
    fills[]
    active_orders
    done
    event_index
```

### Required simulator operations

```text
reset(seed, config) -> MarketObservation
submit(action) -> accepted_order_ids
advance_until(next_decision_time) -> StepResult
cancel(order_ids) -> cancellation_events
liquidate(inventory, policy) -> fills and terminal result
event_tape() -> serializable replay data
```

The control loop is:

```text
observation = simulator.reset(seed, config)
while not observation.session_end:
    state = engine.state()
    action = strategy.compute_action(observation, state)
    simulator.submit(action)
    result = simulator.advance_until(next_decision_time)
    engine.apply_market_events(result.market_events)
    engine.apply_fills(result.fills)
    observation = result.observation
engine.liquidate(state.inventory, liquidation_policy)
```

In the order-book mode, the simulator determines fills through ordinary
matching rules. In intensity mode, the simulator samples fills from the
documented intensity model. Both modes emit the same `Fill` schema so the
accounting layer is independent of execution details.

### Replay requirements

Every experiment saves:

- simulator configuration and random seed;
- event tape or its deterministic seed and generator version;
- strategy name and all parameters;
- every observation used by the strategy;
- every quote, cancel, replacement, fill, risk decision, and liquidation.
- a manifest containing the Python/platform environment, path-independent
  source digests, external-simulator digest where applicable, and input-tape
  digest where applicable.

Replaying a saved run must reproduce actions, fills, inventory, and PnL.

## 12. Strategy interface

Strategies are pure decision modules as far as possible:

```text
compute_action(observation, agent_state, strategy_config) -> QuoteAction
```

They do not mutate cash or inventory. The engine applies fills and risk gates.
This separation lets the same accounting, simulator, and tests evaluate every
strategy fairly.

Required baseline strategies:

1. `NaiveSymmetricMM`;
2. `InventorySkewMM`;
3. `AvellanedaStoikovMM`.

Implemented adaptive strategy:

4. `MicrostructureAdaptiveMM`.

Future variants:

5. calibrated historical-flow policy;
6. market-impact-aware policy;
7. multi-asset inventory policy.

## 13. Metrics and experiment design

Every run and every Monte Carlo batch reports:

### PnL and risk

- final total and mark-to-market PnL;
- spread capture, inventory PnL, fees, adverse selection, and liquidation;
- mean and standard deviation of inventory;
- mean squared inventory exposure `sum q_t^2 * dt`;
- maximum absolute inventory and time at inventory limits;
- maximum drawdown and terminal inventory;
- p5, p50, and p95 final PnL;
- mean PnL and a Sharpe-like statistic, labelled cautiously because simulated
  PnL need not be normal.

### Execution quality

- number of quotes and fills;
- fill rate and quote-to-fill ratio;
- average quoted spread and realized spread;
- average holding time;
- average queue position where available;
- adverse-selection move after fills;
- turnover and cancel/replace rate.
- PnL per second and per traded unit;
- filled-quantity rate, quote-to-trade ratio, inventory RMS, and 5% expected
  shortfall;
- paired common-random-number PnL differences, bootstrap intervals, and the
  fraction of paths on which a candidate beats the baseline.

### Controlled comparisons

Use identical initial conditions and common random numbers when comparing
strategies. The first experiment matrix is:

| Experiment | Sweep | Main question |
| --- | --- | --- |
| Baseline strategy comparison | four strategies | What changes when inventory, volatility, and flow features enter quoting? |
| Risk aversion | `gamma` | Does larger risk aversion reduce inventory variance? |
| Volatility | low to high regimes | Do adaptive quotes widen and protect wealth? |
| Latency | zero to stressed | How quickly does realized spread deteriorate? |
| Fill intensity | `A`, `k` | How sensitive is performance to execution assumptions? |
| Flow toxicity | balanced to one-sided | Does adverse selection dominate gross spread capture? |
| Reference price | midprice vs microprice | Does depth information improve execution quality? |

Report confidence intervals across independent Monte Carlo runs rather than
showing only one path. Keep a small deterministic fixture suite separate from
the stochastic performance suite.

## 14. Validation and test plan

### Accounting tests

- One hand-computed bid fill updates cash, inventory, and wealth correctly.
- One hand-computed ask fill updates cash, inventory, and wealth correctly.
- Fees are charged once and with the correct sign.
- The PnL attribution identity closes within tolerance.
- Terminal liquidation leaves zero inventory when configured to flatten.

### Quote validity tests

- Prices are valid integer ticks.
- If both sides exist, `bid < ask`.
- Passive quotes do not cross the market.
- Quantities are nonnegative and no larger than the configured maximum.
- Quote widths stay within configured bounds after rounding.

### Risk tests

- Intentional quote decisions cannot breach `q_max`, including live-order
  worst-case exposure.
- A long position suppresses or removes the inventory-increasing bid at the
  limit; a short position does the analogous thing for the ask.
- Drawdown, loss, abnormal spread, and volatility kill switches transition to
  the correct risk state.
- Skew changes smoothly with inventory away from hard limits.

### Model-property tests

- Increasing `gamma` reduces inventory variance in a controlled stationary
  experiment, within Monte Carlo uncertainty.
- Increasing volatility widens average Avellaneda-Stoikov quotes.
- Increasing quote distance weakly decreases fill probability.
- Increasing queue ahead weakly decreases passive fill probability.
- A fixed seed reproduces the same event tape, actions, fills, and PnL.
- Volatility estimates do not use future returns.

### Integration tests

- Submit, replace, cancel, and partial-fill order ids are consistent.
- Latency delays both new orders and cancels as configured.
- The order-book matching mode produces fills through normal matching rules.
- A replayed event tape produces the original terminal result.

## 15. Implementation milestones

### Milestone 0: repository and configuration skeleton

Deliver a package layout, typed configuration, deterministic seed handling,
structured logging, and a minimal test runner. Exit criterion: a no-op run can
be configured and replayed.

### Milestone 1: accounting, state, and naive quoting

Implement `AgentState`, fill application, wealth, PnL buckets, tick rounding,
quote validity, and `NaiveSymmetricMM`. Build hand-computed accounting tests.
Exit criterion: accounting identity and quote tests pass.

### Milestone 2: inventory skew and hard risk controls

Implement `InventorySkewMM`, target inventory, live-order exposure checks,
maximum inventory, maximum drawdown, and deterministic risk scenarios. Exit
criterion: risk limits cannot be exceeded by intentional actions.

### Milestone 3: volatility estimation and Avellaneda-Stoikov

Implement causal event-time and clock-time EWMA volatility, finite-horizon
reservation price, quote-width formula, and numerical safeguards. Exit
criterion: property tests for `gamma` and volatility direction pass.

### Milestone 4: execution realism and Project 1 integration

Add the simulator adapter, ordinary order-book matching, queue position,
partial fills, latency, event tapes, and deterministic replay. Exit criterion:
the same strategy can run in both intensity and order-book modes through the
same accounting interface.

### Milestone 5: research experiments and report

Build the experiment runner, Monte Carlo batches, confidence intervals, PnL
attribution plots, inventory and drawdown plots, parameter sweeps, stress
tests, and the final technical report. Exit criterion: four
strategies are compared on identical paths across multiple regimes and every
claim is backed by a plot or test.

## 16. Initial defaults for the first implementation

To keep the first coding step concrete, start with these defaults and make them
configurable later:

```text
single instrument
discrete decision grid with fixed dt
midprice reference
intensity fill mode
zero latency
fixed passive fee per unit
q_target = 0
finite q_max
fixed order size of one unit in the first fixtures
log-return EWMA with a volatility floor
finite horizon T with terminal flattening
```

The current implementation intentionally excludes market impact, multi-asset
risk, hidden liquidity, and learned parameters. Those features would make
failures harder to diagnose before the core accounting and control loop are
validated.

## 17. Capability matrix

| Capability | Status | Evidence or scope boundary |
| --- | --- | --- |
| Four quoted strategies | Implemented | Unit tests and 100-path comparisons |
| Imbalance, signed flow, toxicity, adaptive width/size | Implemented | Causal external adapter features and policy test |
| Executable external-book inventory marks | Implemented | Ledger closure test and external fill marks |
| Reduced / halted / terminal liquidation controls | Implemented | Shared sanitizer and risk tests; liquidation is terminal |
| Paired CRN comparisons and bootstrap intervals | Implemented | Analysis tests and saved summary payloads |
| Development / holdout parameter selection | Implemented for intensity mode | 30 development and 30 untouched holdout seeds; external tuning remains future work |
| Path-independent experiment manifests | Implemented | JSON manifests with source/input digests |
| Historical calibration | Not included | Requires licensed, documented event data |
| Market impact and participant ownership | Not included | External generator remains synthetic and ownership-limited |
| Separate cancel latency / queue lifetime model | Partial | Decision latency and queue priority are present; cancel latency is not separate |
| Production-scale latency benchmark | Not claimed | Correctness-first benchmark only; tail latency is future work |
