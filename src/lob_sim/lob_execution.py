"""Limit-order-book execution adapter for market-making strategies.

This adapter uses each synthetic market point as a background top-of-book
snapshot, then submits the agent's quote into a real price-time-priority book
for the interval. External market orders consume that book, so queue position,
partial fills, and resting execution prices come from the matching engine.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from random import Random
from statistics import mean

from lob_sim.accounting import Fill, Ledger
from lob_sim.agents.interface import AgentView, MarketMaker, MarketObservation, QuoteAction
from lob_sim.agents.volatility import EventTimeEWMA
from lob_sim.core.book import LimitOrderBook
from lob_sim.core.models import ExecutionReport, OrderRequest, OrderType, Side
from lob_sim.data.event_tape import EventTape
from lob_sim.risk.controls import RiskLimits, sanitize_quote
from lob_sim.simulation import RunResult, SimulationConfig
from lob_sim.synthetic.generator import MarketPath, MarketPoint, generate_market_path


class LimitOrderBookMarketSimulator:
    """Run market makers against ordinary matching-engine executions.

    The background book is rebuilt from each synthetic observation. This is a
    controlled adapter, not a historical L2 replay: it is designed to verify
    that strategy orders flow through matching, queue priority, partial fills,
    and normalized accounting before a richer event tape is connected.
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
        execution_rng = Random(seed + 2_000_003)
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
            )

            active_action = (
                quotes[index - self.config.latency_steps]
                if index >= self.config.latency_steps
                else None
            )
            book = self._background_book(point)
            agent_order_ids = self._submit_agent_quotes(
                book=book,
                action=active_action,
                strategy_name=strategy.name,
                step=index,
                timestamp=point.timestamp,
            )
            tape.append(
                "book_snapshot",
                point.timestamp,
                strategy=strategy.name,
                active_decision_time=active_action.timestamp if active_action else None,
                agent_order_ids=agent_order_ids,
                snapshot=asdict(book.snapshot(timestamp=point.timestamp)),
            )

            reports = self._submit_external_flow(
                book=book,
                point=point,
                interval_seconds=points[index + 1].timestamp - point.timestamp,
                rng=execution_rng,
                step=index,
                )
            interval_agent_fills: list[Fill] = []
            for report in reports:
                tape.append(
                    "external_execution",
                    point.timestamp,
                    order_id=report.order_id,
                    remaining=report.remaining,
                    trades=[asdict(trade) for trade in report.trades],
                )
                for trade in report.trades:
                    if trade.maker_owner != strategy.name:
                        continue
                    fill = Fill(
                        fill_id=f"{strategy.name}-{seed}-trade-{trade.trade_id}",
                        order_id=str(trade.maker_order_id),
                        timestamp=trade.timestamp,
                        side=trade.aggressive_side.opposite,
                        price_ticks=trade.price_ticks,
                        quantity=trade.quantity,
                        reference_price_ticks=point.reference_price_ticks,
                        fee=self._fee(trade.quantity, trade.price_ticks),
                    )
                    ledger.apply_fill(fill)
                    fills.append(fill)
                    interval_agent_fills.append(fill)
                    tape.append("fill", fill.timestamp, fill=asdict(fill))

            next_point = points[index + 1]
            ledger.mark(
                timestamp=next_point.timestamp,
                reference_price_ticks=next_point.reference_price_ticks,
            )
            for fill in interval_agent_fills:
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

        self._liquidate(ledger, points[-1], strategy.name, seed, fills, tape)
        wealth[-1] = ledger.wealth
        inventory[-1] = ledger.inventory
        regular_fills = [fill for fill in fills if not fill.is_liquidation]
        spreads = [quote.quoted_spread_ticks for quote in quotes if quote.quoted_spread_ticks]
        metrics = ledger.metrics()
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

    def _background_book(self, point: MarketPoint) -> LimitOrderBook:
        book = LimitOrderBook()
        book.submit(
            OrderRequest(
                client_order_id=f"background-bid-{point.timestamp}",
                side=Side.BUY,
                quantity=point.bid_size,
                price_ticks=point.best_bid_ticks,
                timestamp=point.timestamp,
                owner="background",
            )
        )
        book.submit(
            OrderRequest(
                client_order_id=f"background-ask-{point.timestamp}",
                side=Side.SELL,
                quantity=point.ask_size,
                price_ticks=point.best_ask_ticks,
                timestamp=point.timestamp,
                owner="background",
            )
        )
        book.assert_invariants()
        return book

    @staticmethod
    def _submit_agent_quotes(
        *,
        book: LimitOrderBook,
        action: QuoteAction | None,
        strategy_name: str,
        step: int,
        timestamp: float,
    ) -> dict[str, int]:
        if action is None:
            return {}
        order_ids: dict[str, int] = {}
        if action.bid_price_ticks is not None and action.bid_size:
            report = book.submit(
                OrderRequest(
                    client_order_id=f"{strategy_name}-{step}-bid",
                    side=Side.BUY,
                    quantity=action.bid_size,
                    price_ticks=action.bid_price_ticks,
                    timestamp=timestamp,
                    owner=strategy_name,
                )
            )
            order_ids["bid"] = report.order_id
        if action.ask_price_ticks is not None and action.ask_size:
            report = book.submit(
                OrderRequest(
                    client_order_id=f"{strategy_name}-{step}-ask",
                    side=Side.SELL,
                    quantity=action.ask_size,
                    price_ticks=action.ask_price_ticks,
                    timestamp=timestamp,
                    owner=strategy_name,
                )
            )
            order_ids["ask"] = report.order_id
        book.assert_invariants()
        return order_ids

    def _submit_external_flow(
        self,
        *,
        book: LimitOrderBook,
        point: MarketPoint,
        interval_seconds: float,
        rng: Random,
        step: int,
    ) -> list:
        reports = []
        sell_probability = 1.0 - math.exp(
            -self.config.fill_intensity
            * max(0.0, 1.0 - self.config.flow_bias)
            * interval_seconds
        )
        buy_probability = 1.0 - math.exp(
            -self.config.fill_intensity
            * max(0.0, 1.0 + self.config.flow_bias)
            * interval_seconds
        )
        sell_uniform = rng.random()
        buy_uniform = rng.random()
        if sell_uniform < sell_probability:
            reports.append(
                book.submit(
                    OrderRequest(
                        client_order_id=f"flow-sell-{step}",
                        side=Side.SELL,
                        quantity=max(1, point.bid_size // 2),
                        order_type=OrderType.MARKET,
                        timestamp=point.timestamp,
                        owner="external-seller",
                    )
                )
            )
        if buy_uniform < buy_probability:
            reports.append(
                book.submit(
                    OrderRequest(
                        client_order_id=f"flow-buy-{step}",
                        side=Side.BUY,
                        quantity=max(1, point.ask_size // 2),
                        order_type=OrderType.MARKET,
                        timestamp=point.timestamp,
                        owner="external-buyer",
                    )
                )
            )
        book.assert_invariants()
        return reports

    def _fee(self, quantity: int, price_ticks: int) -> float:
        notional = quantity * price_ticks * self.config.tick_value
        return self.config.fee_per_unit * quantity + self.config.fee_rate * notional

    def _liquidate(
        self,
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


class PersistentLimitOrderBookMarketSimulator(LimitOrderBookMarketSimulator):
    """Persistent synthetic L2 variant with delayed quote replacement.

    Background liquidity is replenished by ordinary limit orders at each market
    point. Those orders may cross stale liquidity and therefore create normal
    matching-engine trades, including fills of stale agent quotes.
    """

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
        execution_rng = Random(seed + 3_000_003)
        points = market_path.points
        first = points[0]
        book = self._background_book(first)
        ledger.mark(timestamp=first.timestamp, reference_price_ticks=first.reference_price_ticks)
        timestamps = [first.timestamp]
        references = [first.reference_price_ticks]
        wealth = [ledger.wealth]
        inventory = [ledger.inventory]
        quotes: list[QuoteAction] = []
        fills: list[Fill] = []
        active_agent_orders: set[int] = set()

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
            tape.append("quote", point.timestamp, strategy=strategy.name, action=asdict(action))

            arriving_action = (
                quotes[index - self.config.latency_steps]
                if index >= self.config.latency_steps
                else None
            )
            interval_fills: list[Fill] = []
            if arriving_action is not None:
                for order_id in active_agent_orders:
                    book.cancel(order_id)
                active_agent_orders.clear()
                agent_ids, agent_reports = self._submit_agent_quotes_with_reports(
                    book=book,
                    action=arriving_action,
                    strategy_name=strategy.name,
                    step=index,
                    timestamp=point.timestamp,
                )
                active_agent_orders.update(agent_ids)
                interval_fills.extend(
                    self._apply_agent_trades(
                        reports=agent_reports,
                        strategy_name=strategy.name,
                        seed=seed,
                        reference_price_ticks=point.reference_price_ticks,
                        ledger=ledger,
                        fills=fills,
                        tape=tape,
                        event_kind="agent_submission_execution",
                        event_timestamp=point.timestamp,
                    )
                )
                tape.append(
                    "quote_arrival",
                    point.timestamp,
                    decision_time=arriving_action.timestamp,
                    active_order_ids=sorted(active_agent_orders),
                )

            if index > 0:
                background_reports = self._replenish_background(book, point, index)
                interval_fills.extend(
                    self._apply_agent_trades(
                        reports=background_reports,
                        strategy_name=strategy.name,
                        seed=seed,
                        reference_price_ticks=point.reference_price_ticks,
                        ledger=ledger,
                        fills=fills,
                        tape=tape,
                        event_kind="background_replenishment",
                        event_timestamp=point.timestamp,
                    )
                )

            external_reports = self._submit_external_flow(
                book=book,
                point=point,
                interval_seconds=points[index + 1].timestamp - point.timestamp,
                rng=execution_rng,
                step=index,
            )
            interval_fills.extend(
                self._apply_agent_trades(
                    reports=external_reports,
                    strategy_name=strategy.name,
                    seed=seed,
                    reference_price_ticks=point.reference_price_ticks,
                    ledger=ledger,
                    fills=fills,
                    tape=tape,
                    event_kind="external_execution",
                    event_timestamp=point.timestamp,
                )
            )
            book.assert_invariants()

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

        self._liquidate(ledger, points[-1], strategy.name, seed, fills, tape)
        wealth[-1] = ledger.wealth
        inventory[-1] = ledger.inventory
        regular_fills = [fill for fill in fills if not fill.is_liquidation]
        spreads = [quote.quoted_spread_ticks for quote in quotes if quote.quoted_spread_ticks]
        metrics = ledger.metrics()
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

    def _replenish_background(
        self,
        book: LimitOrderBook,
        point: MarketPoint,
        step: int,
    ) -> list[ExecutionReport]:
        reports = [
            book.submit(
                OrderRequest(
                    client_order_id=f"replenish-bid-{step}",
                    side=Side.BUY,
                    quantity=point.bid_size,
                    price_ticks=point.best_bid_ticks,
                    timestamp=point.timestamp,
                    owner="background",
                )
            ),
            book.submit(
                OrderRequest(
                    client_order_id=f"replenish-ask-{step}",
                    side=Side.SELL,
                    quantity=point.ask_size,
                    price_ticks=point.best_ask_ticks,
                    timestamp=point.timestamp,
                    owner="background",
                )
            ),
        ]
        return reports

    @staticmethod
    def _submit_agent_quotes_with_reports(
        *,
        book: LimitOrderBook,
        action: QuoteAction,
        strategy_name: str,
        step: int,
        timestamp: float,
    ) -> tuple[set[int], list[ExecutionReport]]:
        active_ids: set[int] = set()
        reports: list[ExecutionReport] = []
        if action.bid_price_ticks is not None and action.bid_size:
            report = book.submit(
                OrderRequest(
                    client_order_id=f"{strategy_name}-persistent-{step}-bid",
                    side=Side.BUY,
                    quantity=action.bid_size,
                    price_ticks=action.bid_price_ticks,
                    timestamp=timestamp,
                    owner=strategy_name,
                )
            )
            reports.append(report)
            if report.remaining:
                active_ids.add(report.order_id)
        if action.ask_price_ticks is not None and action.ask_size:
            report = book.submit(
                OrderRequest(
                    client_order_id=f"{strategy_name}-persistent-{step}-ask",
                    side=Side.SELL,
                    quantity=action.ask_size,
                    price_ticks=action.ask_price_ticks,
                    timestamp=timestamp,
                    owner=strategy_name,
                )
            )
            reports.append(report)
            if report.remaining:
                active_ids.add(report.order_id)
        return active_ids, reports

    def _apply_agent_trades(
        self,
        *,
        reports: list[ExecutionReport],
        strategy_name: str,
        seed: int,
        reference_price_ticks: float,
        ledger: Ledger,
        fills: list[Fill],
        tape: EventTape,
        event_kind: str,
        event_timestamp: float,
    ) -> list[Fill]:
        interval_fills: list[Fill] = []
        for report in reports:
            tape.append(
                event_kind,
                report.trades[0].timestamp if report.trades else event_timestamp,
                order_id=report.order_id,
                remaining=report.remaining,
                trades=[asdict(trade) for trade in report.trades],
            )
            for trade in report.trades:
                if trade.maker_owner != strategy_name:
                    continue
                fill = Fill(
                    fill_id=f"{strategy_name}-{seed}-trade-{trade.trade_id}",
                    order_id=str(trade.maker_order_id),
                    timestamp=trade.timestamp,
                    side=trade.aggressive_side.opposite,
                    price_ticks=trade.price_ticks,
                    quantity=trade.quantity,
                    reference_price_ticks=reference_price_ticks,
                    fee=self._fee(trade.quantity, trade.price_ticks),
                )
                ledger.apply_fill(fill)
                fills.append(fill)
                interval_fills.append(fill)
                tape.append("fill", fill.timestamp, fill=asdict(fill))
        return interval_fills
