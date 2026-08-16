"""Execution adapter for the standalone ``Limit_Order_Book_Simulator``.

The project-1 simulator is intentionally kept as a separate repository.  This
module loads it from a user-supplied path and translates its event-driven
matching results into the market-making engine's accounting and ``RunResult``
contracts.  The adapter is optional: the core package remains dependency-free
and the existing intensity/persistent-book experiments continue to run without
the sibling repository.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from lob_sim.accounting import Fill, Ledger
from lob_sim.agents.interface import AgentView, MarketMaker, MarketObservation, QuoteAction
from lob_sim.agents.volatility import EventTimeEWMA
from lob_sim.core.models import Side
from lob_sim.data.event_tape import EventTape
from lob_sim.data.external_tape import ExternalEventTape
from lob_sim.risk.controls import RiskLimits, sanitize_quote
from lob_sim.simulation import RunResult, SimulationConfig
from lob_sim.synthetic.generator import MarketPath, MarketPoint, generate_market_path


@dataclass(frozen=True, slots=True)
class ExternalLOBModules:
    """Runtime handles imported from the external simulator package."""

    Event: Any
    LimitOrderBook: Any
    OrderFlowConfig: Any
    RegimeGenerator: Any


def load_external_lob_modules(simulator_root: str | Path) -> ExternalLOBModules:
    """Load the external simulator without making it a package dependency.

    ``simulator_root`` may point at the repository root or directly at its
    ``src`` directory.  Imports are cached by Python after the first call, so
    repeated Monte Carlo runs do not reload the matching engine.
    """

    root = Path(simulator_root).expanduser().resolve()
    source_roots = [root / "src", root]
    source_root = next(
        (
            candidate
            for candidate in source_roots
            if (candidate / "qr_platform" / "__init__.py").is_file()
        ),
        None,
    )
    if source_root is None:
        raise FileNotFoundError(
            f"Could not find qr_platform package below {root}; expected {root}/src/qr_platform"
        )

    existing = sys.modules.get("qr_platform")
    existing_file = getattr(existing, "__file__", None)
    if existing_file is not None and not Path(existing_file).resolve().is_relative_to(source_root):
        raise ImportError(
            "A different qr_platform package is already imported; start a fresh process "
            "before switching external simulator roots."
        )
    source_text = str(source_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)

    try:
        package = importlib.import_module("qr_platform")
        synthetic = importlib.import_module("qr_platform.synthetic")
    except ImportError as exc:
        raise ImportError(
            "The external simulator could not be imported. Install its declared runtime "
            "dependencies in the active environment or use the engine's built-in simulator."
        ) from exc

    return ExternalLOBModules(
        Event=package.Event,
        LimitOrderBook=package.LimitOrderBook,
        OrderFlowConfig=synthetic.OrderFlowConfig,
        RegimeGenerator=synthetic.RegimeGenerator,
    )


class ExternalLimitOrderBookMarketSimulator:
    """Run a market maker against Project 1's event-driven matching engine.

    The external engine supplies the exchange state transition: all external
    limits, markets, cancellations, partial fills, and queue priority are
    processed by ``qr_platform.LimitOrderBook``.  The reference price path is
    still exogenous so strategy comparisons can separate inventory/execution
    effects from market-impact research.

    Agent orders are protected from synthetic background cancellations because
    the external generator has no participant-owner filter.  They can still be
    filled by external market and crossing limit events.  Requoting is modeled
    as cancel-and-new, which intentionally resets time priority.
    """

    def __init__(
        self,
        config: SimulationConfig,
        simulator_root: str | Path,
        *,
        snapshot_depth: int = 5,
        order_flow_config: Any | dict[str, Any] | None = None,
    ) -> None:
        if snapshot_depth <= 0:
            raise ValueError("snapshot_depth must be positive")
        self.config = config
        self.simulator_root = Path(simulator_root)
        self.snapshot_depth = snapshot_depth
        self.order_flow_config = order_flow_config

    def run(
        self,
        strategy: MarketMaker,
        *,
        seed: int,
        path: MarketPath | None = None,
        risk_limits: RiskLimits | None = None,
        order_flow_config: Any | dict[str, Any] | None = None,
        external_event_tape: ExternalEventTape | None = None,
    ) -> RunResult:
        modules = load_external_lob_modules(self.simulator_root)
        if external_event_tape is not None:
            if path is not None and path != external_event_tape.market_path:
                raise ValueError("path and external_event_tape.market_path must match")
            market_path = external_event_tape.market_path
        else:
            market_path = path or generate_market_path(self.config, seed)
        if len(market_path.points) < 2:
            raise ValueError("market path must contain at least two points")
        limits = risk_limits or RiskLimits(
            q_max=self.config.q_max,
            max_order_size=1,
            min_half_width_ticks=1.0,
            max_half_width_ticks=50.0,
            max_market_spread_ticks=self.config.max_market_spread_ticks,
        )

        points = market_path.points
        first = points[0]
        book = modules.LimitOrderBook(
            check_invariants=True,
            full_audit_interval=0,
            record_trades=True,
            retain_order_history=True,
        )
        self._seed_background_book(book, modules, first)
        if external_event_tape is None:
            flow_config = self._flow_config(
                modules,
                seed=seed,
                point=first,
                override=(
                    order_flow_config
                    if order_flow_config is not None
                    else self.order_flow_config
                ),
            )
            external_events = iter(
                modules.RegimeGenerator(flow_config).generate(len(points) - 1, book)
            )
            external_tape_mode = "generated"
            external_tape_digest = None
        else:
            external_events = iter(
                self._event_from_record(modules, record)
                for record in external_event_tape.events
            )
            external_tape_mode = "replay"
            external_tape_digest = external_event_tape.digest

        ledger = Ledger(initial_cash=self.config.initial_cash, tick_value=self.config.tick_value)
        volatility = EventTimeEWMA(
            decay_seconds=self.config.volatility_decay_seconds,
            floor_ticks=self.config.volatility_floor_ticks,
        )
        tape = EventTape()
        timestamps = [first.timestamp]
        references = [first.reference_price_ticks]
        wealth = [ledger.wealth]
        inventory = [ledger.inventory]
        quotes: list[QuoteAction] = []
        fills: list[Fill] = []
        live_agent_order_ids: set[str] = set()
        agent_order_sides: dict[str, Side] = {}
        previous_snapshot: Any | None = None
        initial_snapshot = book.snapshot(timestamp=0, depth=1)
        initial_bid, initial_ask = self._executable_marks(initial_snapshot, first)
        ledger.mark(
            timestamp=first.timestamp,
            reference_price_ticks=first.reference_price_ticks,
            executable_bid_ticks=initial_bid,
            executable_ask_ticks=initial_ask,
        )
        tape.append(
            "external_tape",
            first.timestamp,
            mode=external_tape_mode,
            digest=external_tape_digest,
            event_count=len(points) - 1,
        )
        for index, point in enumerate(points[:-1]):
            external_timestamp = index + 1
            snapshot = book.snapshot(timestamp=external_timestamp, depth=self.snapshot_depth)
            sigma = volatility.update(
                timestamp=point.timestamp,
                price_ticks=point.reference_price_ticks,
            )
            observation = self._observation(
                snapshot,
                point,
                sigma,
                flow_features=self._flow_features(snapshot, previous_snapshot),
            )
            state = AgentView(
                timestamp=point.timestamp,
                inventory=ledger.inventory,
                cash=ledger.cash,
                q_target=self.config.q_target,
                q_scale=self.config.q_scale,
                q_max=limits.q_max,
                drawdown=ledger.drawdown,
                risk_status=self._risk_status(ledger, observation, limits),
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
                book_event_timestamp=external_timestamp,
            )

            active_action = (
                quotes[index - self.config.latency_steps]
                if index >= self.config.latency_steps
                else None
            )
            self._cancel_agent_orders(
                book,
                modules,
                live_agent_order_ids,
                timestamp=external_timestamp,
                strategy_name=strategy.name,
                tape=tape,
                wall_timestamp=point.timestamp,
            )
            submission_trades = self._submit_agent_quote(
                book,
                modules,
                active_action,
                strategy_name=strategy.name,
                step=index,
                timestamp=external_timestamp,
                live_order_ids=live_agent_order_ids,
                order_sides=agent_order_sides,
            )
            submission_bid, submission_ask = self._executable_marks(
                book.snapshot(timestamp=external_timestamp, depth=1), point
            )
            interval_fills = self._apply_agent_trades(
                submission_trades,
                agent_order_sides=agent_order_sides,
                strategy_name=strategy.name,
                seed=seed,
                reference_price=point.reference_price_ticks,
                timestamp=point.timestamp,
                ledger=ledger,
                fills=fills,
                executable_bid=submission_bid,
                executable_ask=submission_ask,
            )
            self._refresh_live_ids(book, live_agent_order_ids)

            event = next(external_events)
            processed_event, event_trades = self._process_external_event(
                book,
                modules,
                event,
                live_agent_order_ids=live_agent_order_ids,
            )
            tape.append(
                "external_event",
                point.timestamp,
                event=asdict(event),
                processed_event=asdict(processed_event),
                trades=[asdict(trade) for trade in event_trades],
            )
            after = book.snapshot(timestamp=external_timestamp, depth=self.snapshot_depth)
            event_bid, event_ask = self._executable_marks(after, point)
            interval_fills.extend(
                self._apply_agent_trades(
                    event_trades,
                    agent_order_sides=agent_order_sides,
                    strategy_name=strategy.name,
                    seed=seed,
                    reference_price=point.reference_price_ticks,
                    timestamp=point.timestamp,
                    ledger=ledger,
                    fills=fills,
                    executable_bid=event_bid,
                    executable_ask=event_ask,
                )
            )
            self._refresh_live_ids(book, live_agent_order_ids)
            book.assert_light_invariants()
            previous_snapshot = snapshot
            tape.append(
                "book_snapshot",
                point.timestamp,
                snapshot=asdict(after),
                active_agent_order_ids=sorted(live_agent_order_ids),
            )

            next_point = points[index + 1]
            ledger.mark(
                timestamp=next_point.timestamp,
                reference_price_ticks=next_point.reference_price_ticks,
                executable_bid_ticks=event_bid,
                executable_ask_ticks=event_ask,
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

        liquidation_fills, liquidation_unfilled = self._liquidate(
            book,
            modules,
            ledger=ledger,
            point=points[-1],
            strategy_name=strategy.name,
            seed=seed,
            live_agent_order_ids=live_agent_order_ids,
            tape=tape,
        )
        fills.extend(liquidation_fills)
        book.audit()
        if liquidation_fills:
            wealth[-1] = ledger.wealth
            inventory[-1] = ledger.inventory

        regular_fills = [fill for fill in fills if not fill.is_liquidation]
        queue_ahead = [fill.queue_ahead_at_entry for fill in regular_fills]
        spreads = [quote.quoted_spread_ticks for quote in quotes if quote.quoted_spread_ticks]
        metrics = ledger.metrics()
        metrics.update(
            {
                "quote_count": float(len(quotes)),
                "fill_count": float(len(regular_fills)),
                "liquidation_count": float(len(liquidation_fills)),
                "fill_rate": len(regular_fills) / len(quotes) if quotes else 0.0,
                "average_quoted_spread_ticks": sum(spreads) / len(spreads) if spreads else 0.0,
                "max_abs_inventory": float(max(abs(value) for value in inventory)),
                "mean_abs_inventory": sum(abs(value) for value in inventory) / len(inventory),
                "time_at_inventory_limit": sum(
                    abs(value) >= limits.q_max for value in inventory
                )
                / len(inventory),
                "external_event_count": float(book.event_count),
                "external_trade_count": float(book.trade_count),
                "external_limit_count": float(book.limit_count),
                "external_market_count": float(book.market_count),
                "external_cancel_count": float(book.cancel_count),
                "external_successful_cancel_count": float(book.successful_cancel_count),
                "external_unfilled_market_quantity": float(book.unfilled_market_quantity),
                "terminal_liquidation_unfilled": float(liquidation_unfilled),
                "mean_queue_ahead_at_entry": (
                    sum(queue_ahead) / len(queue_ahead) if queue_ahead else 0.0
                ),
                "max_queue_ahead_at_entry": float(max(queue_ahead, default=0)),
            }
        )
        tape.append(
            "terminal",
            points[-1].timestamp,
            final_pnl=ledger.total_pnl,
            final_inventory=ledger.inventory,
            accounting_error=ledger.accounting_error,
            external_event_count=book.event_count,
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

    @staticmethod
    def _seed_background_book(book: Any, modules: ExternalLOBModules, point: MarketPoint) -> None:
        book.process(
            modules.Event(
                "limit",
                0,
                side="buy",
                order_id="background-initial-bid",
                price=point.best_bid_ticks,
                quantity=point.bid_size,
                event_id="initial:bid",
            )
        )
        book.process(
            modules.Event(
                "limit",
                0,
                side="sell",
                order_id="background-initial-ask",
                price=point.best_ask_ticks,
                quantity=point.ask_size,
                event_id="initial:ask",
            )
        )

    @staticmethod
    def _flow_config(
        modules: ExternalLOBModules,
        *,
        seed: int,
        point: MarketPoint,
        override: Any | dict[str, Any] | None,
    ) -> Any:
        if override is not None and not isinstance(override, dict):
            return override
        values = dict(override or {})
        values.setdefault("seed", seed)
        values.setdefault("start_price", point.reference_price_ticks)
        values.setdefault("tick_spread", max(1, point.best_ask_ticks - point.best_bid_ticks))
        values.setdefault("max_quantity", max(1, 2 * max(point.bid_size, point.ask_size)))
        values.setdefault("regime", "liquid")
        return modules.OrderFlowConfig(**values)

    @staticmethod
    def _observation(
        snapshot: Any,
        point: MarketPoint,
        volatility: float,
        *,
        flow_features: dict[str, float] | None = None,
    ) -> MarketObservation:
        best_bid = snapshot.best_bid if snapshot.best_bid is not None else point.best_bid_ticks
        best_ask = snapshot.best_ask if snapshot.best_ask is not None else point.best_ask_ticks
        if best_bid >= best_ask:
            best_bid, best_ask = point.best_bid_ticks, point.best_ask_ticks
        bid_size = snapshot.bid_levels[0][1] if snapshot.bid_levels else point.bid_size
        ask_size = snapshot.ask_levels[0][1] if snapshot.ask_levels else point.ask_size
        return MarketObservation(
            timestamp=point.timestamp,
            best_bid_ticks=best_bid,
            best_ask_ticks=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            volatility_ticks=volatility,
            last_trade_price_ticks=None,
            fair_price_ticks=point.reference_price_ticks,
            **(flow_features or {}),
        )

    @staticmethod
    def _executable_marks(snapshot: Any, point: MarketPoint) -> tuple[float, float]:
        bid = snapshot.best_bid if snapshot.best_bid is not None else point.best_bid_ticks
        ask = snapshot.best_ask if snapshot.best_ask is not None else point.best_ask_ticks
        if bid >= ask:
            bid, ask = point.best_bid_ticks, point.best_ask_ticks
        return float(bid), float(ask)

    @staticmethod
    def _flow_features(snapshot: Any, previous: Any | None) -> dict[str, float]:
        if previous is None:
            return {}
        event_delta = max(snapshot.event_count - previous.event_count, 1)
        buy_delta = snapshot.buy_trade_quantity - previous.buy_trade_quantity
        sell_delta = snapshot.sell_trade_quantity - previous.sell_trade_quantity
        traded = buy_delta + sell_delta
        signed_flow = (buy_delta - sell_delta) / traded if traded else 0.0
        top_depth = max(snapshot.bid_depth + snapshot.ask_depth, 1)
        toxicity = min(1.0, abs(signed_flow) * (1.0 + traded / top_depth))
        return {
            "signed_trade_flow": signed_flow,
            "cancellation_rate": max(
                0.0,
                (snapshot.cancel_count - previous.cancel_count) / event_delta,
            ),
            "queue_depletion_rate": max(
                0.0,
                (snapshot.queue_depletion_count - previous.queue_depletion_count)
                / event_delta,
            ),
            "toxicity_score": toxicity,
        }

    @staticmethod
    def _cancel_agent_orders(
        book: Any,
        modules: ExternalLOBModules,
        live_order_ids: set[str],
        *,
        timestamp: int,
        strategy_name: str,
        tape: EventTape,
        wall_timestamp: float,
    ) -> None:
        for order_id in tuple(sorted(live_order_ids)):
            if order_id not in book.active_orders:
                live_order_ids.discard(order_id)
                continue
            event = modules.Event(
                "cancel",
                timestamp,
                order_id=order_id,
                event_id=f"{strategy_name}:cancel:{timestamp}:{order_id}",
            )
            book.process(event)
            live_order_ids.discard(order_id)
            tape.append("quote_cancel", wall_timestamp, order_id=order_id)

    @staticmethod
    def _submit_agent_quote(
        book: Any,
        modules: ExternalLOBModules,
        action: QuoteAction | None,
        *,
        strategy_name: str,
        step: int,
        timestamp: int,
        live_order_ids: set[str],
        order_sides: dict[str, Side],
    ) -> list[Any]:
        if action is None:
            return []
        trades: list[Any] = []
        for side, price, quantity, suffix in (
            ("buy", action.bid_price_ticks, action.bid_size, "bid"),
            ("sell", action.ask_price_ticks, action.ask_size, "ask"),
        ):
            if price is None or quantity <= 0:
                continue
            order_id = f"{strategy_name}:quote:{step}:{suffix}"
            order_sides[order_id] = Side.BUY if side == "buy" else Side.SELL
            event = modules.Event(
                "limit",
                timestamp,
                side=side,
                order_id=order_id,
                price=price,
                quantity=quantity,
                event_id=f"{strategy_name}:quote-event:{step}:{suffix}",
            )
            trades.extend(book.process(event))
            if order_id in book.active_orders:
                live_order_ids.add(order_id)
        return trades

    @staticmethod
    def _process_external_event(
        book: Any,
        modules: ExternalLOBModules,
        event: Any,
        *,
        live_agent_order_ids: set[str],
    ) -> tuple[Any, list[Any]]:
        processed = event
        if event.event_type == "cancel" and event.order_id in live_agent_order_ids:
            processed = modules.Event(
                "cancel",
                event.timestamp,
                order_id=f"protected-agent-cancel:{event.timestamp}",
                event_id=event.event_id,
            )
        return processed, book.process(processed)

    @staticmethod
    def _event_from_record(modules: ExternalLOBModules, record: dict[str, Any]) -> Any:
        fields = {
            key: record.get(key)
            for key in (
                "event_type",
                "timestamp",
                "side",
                "order_id",
                "price",
                "quantity",
                "event_id",
                "receive_timestamp",
            )
        }
        return modules.Event(**fields)

    @staticmethod
    def _refresh_live_ids(book: Any, live_order_ids: set[str]) -> None:
        live_order_ids.intersection_update(book.active_orders)

    def _apply_agent_trades(
        self,
        trades: list[Any],
        *,
        agent_order_sides: dict[str, Side],
        strategy_name: str,
        seed: int,
        reference_price: int,
        timestamp: float,
        ledger: Ledger,
        fills: list[Fill],
        executable_bid: float,
        executable_ask: float,
    ) -> list[Fill]:
        interval_fills: list[Fill] = []
        for trade in trades:
            order_id: str | None = None
            side: Side | None = None
            queue_ahead = 0
            queue_position = 1
            if trade.resting_order_id in agent_order_sides:
                order_id = trade.resting_order_id
                side = Side.BUY if trade.aggressor_side == "sell" else Side.SELL
                queue_ahead = trade.resting_quantity_ahead_at_entry
                queue_position = trade.resting_queue_position_at_execution
            elif trade.incoming_order_id in agent_order_sides:
                order_id = trade.incoming_order_id
                side = agent_order_sides[order_id]
            if order_id is None or side is None:
                continue
            fill = Fill(
                fill_id=f"{strategy_name}-{seed}-external-trade-{trade.sequence_number}",
                order_id=order_id,
                timestamp=timestamp,
                side=side,
                price_ticks=trade.price,
                quantity=trade.quantity,
                reference_price_ticks=reference_price,
                fee=self._fee(trade.quantity, trade.price),
                queue_ahead_at_entry=queue_ahead,
                queue_position_at_execution=queue_position,
                execution_source="external_qr_platform",
                executable_bid_ticks=executable_bid,
                executable_ask_ticks=executable_ask,
            )
            ledger.apply_fill(fill)
            fills.append(fill)
            interval_fills.append(fill)
        return interval_fills

    def _liquidate(
        self,
        book: Any,
        modules: ExternalLOBModules,
        *,
        ledger: Ledger,
        point: MarketPoint,
        strategy_name: str,
        seed: int,
        live_agent_order_ids: set[str],
        tape: EventTape,
    ) -> tuple[list[Fill], int]:
        if ledger.inventory == 0:
            return [], 0
        terminal_timestamp = book.event_count + 1
        self._cancel_agent_orders(
            book,
            modules,
            live_agent_order_ids,
            timestamp=terminal_timestamp,
            strategy_name=strategy_name,
            tape=tape,
            wall_timestamp=point.timestamp,
        )
        quantity = abs(ledger.inventory)
        side = "sell" if ledger.inventory > 0 else "buy"
        liquidation_id = f"{strategy_name}:liquidation:{seed}"
        event = modules.Event(
            "market",
            terminal_timestamp,
            side=side,
            order_id=liquidation_id,
            quantity=quantity,
            event_id=f"{strategy_name}:liquidation-event:{seed}",
        )
        trades = book.process(event)
        liquidation_bid, liquidation_ask = self._executable_marks(
            book.snapshot(timestamp=terminal_timestamp, depth=1), point
        )
        liquidation_fills: list[Fill] = []
        matched = 0
        for trade in trades:
            matched += trade.quantity
            fill_side = Side.SELL if side == "sell" else Side.BUY
            fill = Fill(
                fill_id=f"{strategy_name}-{seed}-liquidation-{trade.sequence_number}",
                order_id=liquidation_id,
                timestamp=point.timestamp,
                side=fill_side,
                price_ticks=trade.price,
                quantity=trade.quantity,
                reference_price_ticks=point.reference_price_ticks,
                fee=self._fee(trade.quantity, trade.price),
                is_liquidation=True,
                execution_source="external_qr_platform",
                executable_bid_ticks=liquidation_bid,
                executable_ask_ticks=liquidation_ask,
            )
            ledger.apply_fill(fill)
            liquidation_fills.append(fill)
        residual = quantity - matched
        if residual:
            price = (
                max(1, point.reference_price_ticks - self.config.liquidation_slippage_ticks)
                if side == "sell"
                else point.reference_price_ticks + self.config.liquidation_slippage_ticks
            )
            fallback = Fill(
                fill_id=f"{strategy_name}-{seed}-liquidation-backstop",
                order_id=liquidation_id,
                timestamp=point.timestamp,
                side=Side.SELL if side == "sell" else Side.BUY,
                price_ticks=price,
                quantity=residual,
                reference_price_ticks=point.reference_price_ticks,
                fee=self._fee(residual, price),
                is_liquidation=True,
                execution_source="external_liquidation_backstop",
                executable_bid_ticks=liquidation_bid,
                executable_ask_ticks=liquidation_ask,
            )
            ledger.apply_fill(fallback)
            liquidation_fills.append(fallback)
        tape.append(
            "liquidation",
            point.timestamp,
            event=asdict(event),
            trades=[asdict(trade) for trade in trades],
            matched_quantity=matched,
            backstop_quantity=residual,
        )
        return liquidation_fills, residual

    def _fee(self, quantity: int, price_ticks: int) -> float:
        notional = quantity * price_ticks * self.config.tick_value
        return self.config.fee_per_unit * quantity + self.config.fee_rate * notional

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


def generate_external_event_tape(
    simulator_root: str | Path,
    path: MarketPath,
    *,
    seed: int,
    order_flow_config: Any | dict[str, Any] | None = None,
    toxic_response_ticks: float = 0.0,
) -> ExternalEventTape:
    """Generate and persist a strategy-independent Project 1 event scenario."""

    if len(path.points) < 2:
        raise ValueError("market path must contain at least two points")
    modules = load_external_lob_modules(simulator_root)
    first = path.points[0]
    book = modules.LimitOrderBook(
        check_invariants=True,
        full_audit_interval=0,
        record_trades=False,
        retain_order_history=True,
    )
    ExternalLimitOrderBookMarketSimulator._seed_background_book(book, modules, first)
    flow_config = ExternalLimitOrderBookMarketSimulator._flow_config(
        modules,
        seed=seed,
        point=first,
        override=order_flow_config,
    )
    generator = modules.RegimeGenerator(flow_config)
    events = []
    for event in generator.generate(len(path.points) - 1, book):
        book.process(event)
        events.append(asdict(event))
    book.audit()
    tape = ExternalEventTape(
        market_path=path,
        events=tuple(events),
        metadata={
            "generator": "qr_platform.RegimeGenerator",
            "seed": seed,
        },
    )
    return tape.with_toxic_response(toxic_response_ticks) if toxic_response_ticks else tape
