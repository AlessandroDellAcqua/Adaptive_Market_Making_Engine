"""Deterministic intensity-based market-making simulator."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from random import Random
from statistics import mean

from lob_sim.accounting import Fill, Ledger
from lob_sim.agents.interface import AgentView, MarketMaker, MarketObservation, QuoteAction
from lob_sim.agents.volatility import EventTimeEWMA
from lob_sim.core.models import Side
from lob_sim.data.event_tape import EventTape
from lob_sim.risk.controls import RiskLimits, sanitize_quote
from lob_sim.synthetic.generator import MarketPath, MarketPoint, PathConfig, generate_market_path


@dataclass(frozen=True, slots=True)
class SimulationConfig(PathConfig):
    initial_cash: float = 100_000.0
    tick_value: float = 0.01
    fill_intensity: float = 2.0
    fill_decay_k: float = 0.5
    fee_per_unit: float = 0.005
    fee_rate: float = 0.0
    liquidation_slippage_ticks: int = 2
    q_target: int = 0
    q_scale: float = 10.0
    q_max: int = 10
    volatility_decay_seconds: float = 5.0
    volatility_floor_ticks: float = 0.01
    max_market_spread_ticks: int = 20
    latency_seconds: float = 0.0
    queue_decay_rho: float = 0.0
    queue_scale_units: float = 10.0

    def __post_init__(self) -> None:
        PathConfig.__post_init__(self)
        if self.initial_cash <= 0 or self.tick_value <= 0:
            raise ValueError("cash and tick value must be positive")
        if self.fill_intensity < 0 or self.fill_decay_k <= 0:
            raise ValueError("fill parameters are invalid")
        if self.fee_per_unit < 0 or self.fee_rate < 0:
            raise ValueError("fees cannot be negative")
        if self.liquidation_slippage_ticks < 0 or self.q_max <= 0:
            raise ValueError("liquidation and inventory limits are invalid")
        if self.q_scale <= 0:
            raise ValueError("q_scale must be positive")
        if self.latency_seconds < 0 or self.queue_decay_rho < 0:
            raise ValueError("latency and queue decay must be non-negative")
        if self.queue_scale_units <= 0:
            raise ValueError("queue_scale_units must be positive")

    @property
    def latency_steps(self) -> int:
        return round(self.latency_seconds / self.dt_seconds)


@dataclass(frozen=True, slots=True)
class RunResult:
    strategy_name: str
    seed: int
    timestamps: tuple[float, ...]
    reference_prices_ticks: tuple[int, ...]
    wealth: tuple[float, ...]
    inventory: tuple[int, ...]
    quotes: tuple[QuoteAction, ...]
    fills: tuple[Fill, ...]
    metrics: dict[str, float]
    event_tape: EventTape

    def final_pnl(self) -> float:
        return self.metrics["final_pnl"]


class IntensityMarketSimulator:
    """Run strategies against an exogenous path with stochastic passive fills.

    The simulator uses two independent, fixed execution uniforms at every time
    step. This keeps the execution randomness aligned across strategies even
    when one strategy quotes only one side.
    """

    def __init__(self, config: SimulationConfig) -> None:
        self.config = config

    def run(
        self,
        strategy: MarketMaker,
        *,
        seed: int,
        path: MarketPath | None = None,
        risk_limits: RiskLimits | None = None,
    ) -> RunResult:
        market_path = path or generate_market_path(self.config, seed)
        limits = risk_limits or RiskLimits(
            q_max=self.config.q_max,
            max_order_size=1,
            min_half_width_ticks=1.0,
            max_half_width_ticks=50.0,
            max_market_spread_ticks=self.config.max_market_spread_ticks,
        )
        ledger = Ledger(initial_cash=self.config.initial_cash, tick_value=self.config.tick_value)
        volatility = EventTimeEWMA(
            decay_seconds=self.config.volatility_decay_seconds,
            floor_ticks=self.config.volatility_floor_ticks,
        )
        tape = EventTape()
        execution_rng = Random(seed + 1_000_003)
        points = market_path.points
        first = points[0]
        ledger.mark(timestamp=first.timestamp, reference_price_ticks=first.reference_price_ticks)
        timestamps = [first.timestamp]
        references = [first.reference_price_ticks]
        wealth = [ledger.wealth]
        inventory = [ledger.inventory]
        quotes: list[QuoteAction] = []
        fills: list[Fill] = []

        for index, point in enumerate(points[:-1]):
            sigma = volatility.update(
                timestamp=point.timestamp,
                price_ticks=point.reference_price_ticks,
            )
            observation = MarketObservation(
                timestamp=point.timestamp,
                best_bid_ticks=point.best_bid_ticks,
                best_ask_ticks=point.best_ask_ticks,
                bid_size=point.bid_size,
                ask_size=point.ask_size,
                volatility_ticks=sigma,
                last_trade_price_ticks=point.last_trade_price_ticks,
                fair_price_ticks=point.reference_price_ticks,
            )
            risk_status = self._risk_status(ledger, observation, limits)
            state = AgentView(
                timestamp=point.timestamp,
                inventory=ledger.inventory,
                cash=ledger.cash,
                q_target=self.config.q_target,
                q_scale=self.config.q_scale,
                q_max=limits.q_max,
                drawdown=ledger.drawdown,
                risk_status=risk_status,
            )
            raw = strategy.compute_raw_quote(observation, state)
            action = sanitize_quote(
                raw,
                observation=observation,
                state=state,
                limits=limits,
                strategy_name=strategy.name,
            )
            quotes.append(action)
            tape.append(
                "quote",
                point.timestamp,
                strategy=strategy.name,
                action=asdict(action),
                inventory=ledger.inventory,
                reference_price_ticks=point.reference_price_ticks,
            )

            active_action = (
                quotes[index - self.config.latency_steps]
                if index >= self.config.latency_steps
                else None
            )
            tape.append(
                "quote_arrival",
                point.timestamp,
                decision_time=active_action.timestamp if active_action else None,
                action=asdict(active_action) if active_action else None,
            )

            bid_uniform = execution_rng.random()
            ask_uniform = execution_rng.random()
            interval_fills = self._sample_fills(
                action=active_action,
                point=point,
                interval_seconds=points[index + 1].timestamp - point.timestamp,
                bid_uniform=bid_uniform,
                ask_uniform=ask_uniform,
                strategy_name=strategy.name,
                seed=seed,
                step=index,
            )
            for fill in interval_fills:
                ledger.apply_fill(fill)
                fills.append(fill)
                tape.append("fill", fill.timestamp, fill=asdict(fill))

            next_point = points[index + 1]
            ledger.mark(
                timestamp=next_point.timestamp,
                reference_price_ticks=next_point.reference_price_ticks,
            )
            for fill in interval_fills:
                cost = ledger.record_adverse_selection(
                    fill,
                    future_reference_price_ticks=next_point.reference_price_ticks,
                )
                tape.append(
                    "adverse_selection",
                    next_point.timestamp,
                    fill_id=fill.fill_id,
                    cost=cost,
                )
            timestamps.append(next_point.timestamp)
            references.append(next_point.reference_price_ticks)
            wealth.append(ledger.wealth)
            inventory.append(ledger.inventory)

        self._liquidate(
            ledger=ledger,
            point=points[-1],
            strategy_name=strategy.name,
            seed=seed,
            fills=fills,
            tape=tape,
        )
        wealth[-1] = ledger.wealth
        inventory[-1] = ledger.inventory

        metrics = ledger.metrics()
        regular_fills = [fill for fill in fills if not fill.is_liquidation]
        spreads = [quote.quoted_spread_ticks for quote in quotes if quote.quoted_spread_ticks]
        metrics.update(
            {
                "quote_count": float(len(quotes)),
                "fill_count": float(len(regular_fills)),
                "liquidation_count": float(len(fills) - len(regular_fills)),
                "fill_rate": len(regular_fills) / len(quotes) if quotes else 0.0,
                "average_quoted_spread_ticks": mean(spreads) if spreads else 0.0,
                "max_abs_inventory": float(max(abs(value) for value in inventory)),
                "mean_abs_inventory": mean(abs(value) for value in inventory),
                "time_at_inventory_limit": sum(
                    abs(value) >= limits.q_max for value in inventory
                )
                / len(inventory),
            }
        )
        tape.append(
            "terminal",
            points[-1].timestamp,
            final_pnl=ledger.total_pnl,
            final_inventory=ledger.inventory,
            accounting_error=ledger.accounting_error,
        )
        return RunResult(
            strategy_name=strategy.name,
            seed=seed,
            timestamps=tuple(timestamps),
            reference_prices_ticks=tuple(references),
            wealth=tuple(wealth),
            inventory=tuple(inventory),
            quotes=tuple(quotes),
            fills=tuple(fills),
            metrics=metrics,
            event_tape=tape,
        )

    def _sample_fills(
        self,
        *,
        action: QuoteAction | None,
        point: MarketPoint,
        interval_seconds: float,
        bid_uniform: float,
        ask_uniform: float,
        strategy_name: str,
        seed: int,
        step: int,
    ) -> list[Fill]:
        reference = point.reference_price_ticks
        flow_bias = self.config.flow_bias
        fills: list[Fill] = []
        if action is None:
            return fills
        if action.bid_price_ticks is not None and action.bid_size:
            distance = abs(reference - action.bid_price_ticks)
            probability = fill_probability(
                distance=distance,
                interval_seconds=interval_seconds,
                baseline_intensity=self.config.fill_intensity * max(0.0, 1.0 - flow_bias),
                decay_k=self.config.fill_decay_k,
                queue_ahead=point.bid_size,
                queue_decay_rho=self.config.queue_decay_rho,
                queue_scale=self.config.queue_scale_units,
            )
            if bid_uniform < probability:
                fills.append(self._make_fill(
                    side=Side.BUY,
                    price_ticks=action.bid_price_ticks,
                    quantity=action.bid_size,
                    reference=reference,
                    timestamp=point.timestamp,
                    fee=self._fee(action.bid_size, action.bid_price_ticks),
                    strategy_name=strategy_name,
                    seed=seed,
                    step=step,
                    suffix="B",
                ))
        if action.ask_price_ticks is not None and action.ask_size:
            distance = abs(action.ask_price_ticks - reference)
            probability = fill_probability(
                distance=distance,
                interval_seconds=interval_seconds,
                baseline_intensity=self.config.fill_intensity * max(0.0, 1.0 + flow_bias),
                decay_k=self.config.fill_decay_k,
                queue_ahead=point.ask_size,
                queue_decay_rho=self.config.queue_decay_rho,
                queue_scale=self.config.queue_scale_units,
            )
            if ask_uniform < probability:
                fills.append(self._make_fill(
                    side=Side.SELL,
                    price_ticks=action.ask_price_ticks,
                    quantity=action.ask_size,
                    reference=reference,
                    timestamp=point.timestamp,
                    fee=self._fee(action.ask_size, action.ask_price_ticks),
                    strategy_name=strategy_name,
                    seed=seed,
                    step=step,
                    suffix="A",
                ))
        return fills

    def _make_fill(
        self,
        *,
        side: Side,
        price_ticks: int,
        quantity: int,
        reference: float,
        timestamp: float,
        fee: float,
        strategy_name: str,
        seed: int,
        step: int,
        suffix: str,
    ) -> Fill:
        return Fill(
            fill_id=f"{strategy_name}-{seed}-{step}-{suffix}",
            order_id=f"quote-{strategy_name}-{seed}-{step}-{suffix}",
            timestamp=timestamp,
            side=side,
            price_ticks=price_ticks,
            quantity=quantity,
            reference_price_ticks=reference,
            fee=fee,
        )

    def _fee(self, quantity: int, price_ticks: int) -> float:
        notional = quantity * price_ticks * self.config.tick_value
        return self.config.fee_per_unit * quantity + self.config.fee_rate * notional

    def _liquidate(
        self,
        *,
        ledger: Ledger,
        point: MarketPoint,
        strategy_name: str,
        seed: int,
        fills: list[Fill],
        tape: EventTape,
    ) -> None:
        if ledger.inventory == 0:
            return

        if ledger.inventory > 0:
            side = Side.SELL
            quantity = ledger.inventory
            price = max(1, point.reference_price_ticks - self.config.liquidation_slippage_ticks)
        else:
            side = Side.BUY
            quantity = -ledger.inventory
            price = point.reference_price_ticks + self.config.liquidation_slippage_ticks
        fill = Fill(
            fill_id=f"{strategy_name}-{seed}-liquidation",
            order_id=f"{strategy_name}-{seed}-liquidation",
            timestamp=point.timestamp,
            side=side,
            price_ticks=price,
            quantity=quantity,
            reference_price_ticks=point.reference_price_ticks,
            fee=self._fee(quantity, price),
            is_liquidation=True,
        )
        ledger.apply_fill(fill)
        fills.append(fill)
        tape.append("liquidation", fill.timestamp, fill=asdict(fill))

    @staticmethod
    def _risk_status(ledger: Ledger, observation: MarketObservation, limits: RiskLimits) -> str:
        if (
            ledger.drawdown >= limits.max_drawdown
            or observation.market_spread_ticks > limits.max_market_spread_ticks
            or observation.volatility_ticks > limits.volatility_limit_ticks
        ):
            return "HALTED"
        if abs(ledger.inventory) >= limits.reduced_inventory_fraction * limits.q_max:
            return "REDUCED"
        return "NORMAL"


def fill_probability(
    *,
    distance: float,
    interval_seconds: float,
    baseline_intensity: float,
    decay_k: float,
    queue_ahead: float = 0.0,
    queue_decay_rho: float = 0.0,
    queue_scale: float = 1.0,
) -> float:
    """Poisson fill probability with optional monotone queue-position decay."""

    if distance < 0 or interval_seconds < 0 or baseline_intensity < 0:
        raise ValueError("distance, interval, and intensity must be non-negative")
    if decay_k <= 0 or queue_decay_rho < 0 or queue_scale <= 0 or queue_ahead < 0:
        raise ValueError("fill-decay parameters are invalid")
    queue_factor = math.exp(-queue_decay_rho * queue_ahead / queue_scale)
    intensity = baseline_intensity * math.exp(-decay_k * distance) * queue_factor
    return 1.0 - math.exp(-intensity * interval_seconds)
