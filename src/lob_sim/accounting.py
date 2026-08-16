"""Cash, inventory, mark-to-market wealth, and PnL attribution."""

from __future__ import annotations

from dataclasses import dataclass

from lob_sim.core.models import Side


@dataclass(frozen=True, slots=True)
class Fill:
    """A fill normalized from either the LOB or the intensity simulator."""

    fill_id: str
    order_id: str
    timestamp: float
    side: Side
    price_ticks: int
    quantity: int
    reference_price_ticks: float
    fee: float = 0.0
    is_liquidation: bool = False
    queue_ahead_at_entry: int = 0
    queue_position_at_execution: int = 1
    execution_source: str = "synthetic"
    executable_bid_ticks: float | None = None
    executable_ask_ticks: float | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if self.price_ticks <= 0:
            raise ValueError("fill price must be positive")
        if self.fee < 0:
            raise ValueError("fee must be non-negative")
        if self.queue_ahead_at_entry < 0:
            raise ValueError("queue ahead cannot be negative")
        if self.queue_position_at_execution <= 0:
            raise ValueError("queue position must be positive")
        if not self.execution_source:
            raise ValueError("execution source must be non-empty")
        if self.executable_bid_ticks is not None and self.executable_bid_ticks <= 0:
            raise ValueError("executable bid must be positive")
        if self.executable_ask_ticks is not None and self.executable_ask_ticks <= 0:
            raise ValueError("executable ask must be positive")


@dataclass(slots=True)
class PnLBuckets:
    """Signed, additive PnL buckets plus a diagnostic adverse-selection cost."""

    spread_capture: float = 0.0
    inventory_mark_to_market: float = 0.0
    fees: float = 0.0
    adverse_selection_cost: float = 0.0
    liquidation_pnl: float = 0.0
    executable_mark_adjustment: float = 0.0

    @property
    def additive_total(self) -> float:
        return (
            self.spread_capture
            + self.inventory_mark_to_market
            - self.fees
            + self.liquidation_pnl
            + self.executable_mark_adjustment
        )


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    timestamp: float
    cash: float
    inventory: int
    reference_price_ticks: float
    executable_bid_ticks: float
    executable_ask_ticks: float
    wealth: float
    drawdown: float


class Ledger:
    """Account ledger with explicit fill ordering and PnL attribution.

    The mark-to-market bucket is updated whenever the reference price changes.
    A fill first marks any intervening price move, then changes cash and
    inventory, which makes the accounting identity auditable event by event.
    """

    def __init__(self, *, initial_cash: float, tick_value: float) -> None:
        if tick_value <= 0:
            raise ValueError("tick_value must be positive")
        self.initial_cash = float(initial_cash)
        self.tick_value = float(tick_value)
        self.cash = float(initial_cash)
        self.inventory = 0
        self.reference_price_ticks: float | None = None
        self.executable_bid_ticks: float | None = None
        self.executable_ask_ticks: float | None = None
        self.peak_wealth = float(initial_cash)
        self.pnl = PnLBuckets()
        self.snapshots: list[LedgerSnapshot] = []

    @property
    def wealth(self) -> float:
        if self.reference_price_ticks is None:
            return self.cash
        mark = self._inventory_mark_price()
        return self.cash + self.inventory * mark * self.tick_value

    @property
    def drawdown(self) -> float:
        return self.peak_wealth - self.wealth

    @property
    def total_pnl(self) -> float:
        return self.wealth - self.initial_cash

    @property
    def accounting_error(self) -> float:
        return self.total_pnl - self.pnl.additive_total

    def mark(
        self,
        *,
        timestamp: float,
        reference_price_ticks: float,
        executable_bid_ticks: float | None = None,
        executable_ask_ticks: float | None = None,
    ) -> LedgerSnapshot:
        if reference_price_ticks <= 0:
            raise ValueError("reference price must be positive")
        bid = executable_bid_ticks or reference_price_ticks
        ask = executable_ask_ticks or reference_price_ticks
        if bid <= 0 or ask <= 0 or bid > ask:
            raise ValueError("executable marks must satisfy 0 < bid <= ask")
        if self.reference_price_ticks is not None:
            delta = self._inventory_mark_price(bid=bid, ask=ask) - self._inventory_mark_price()
            self.pnl.inventory_mark_to_market += (
                self.inventory * delta * self.tick_value
            )
        self.reference_price_ticks = reference_price_ticks
        self.executable_bid_ticks = bid
        self.executable_ask_ticks = ask
        self.peak_wealth = max(self.peak_wealth, self.wealth)
        snapshot = LedgerSnapshot(
            timestamp=timestamp,
            cash=self.cash,
            inventory=self.inventory,
            reference_price_ticks=reference_price_ticks,
            executable_bid_ticks=bid,
            executable_ask_ticks=ask,
            wealth=self.wealth,
            drawdown=self.drawdown,
        )
        self.snapshots.append(snapshot)
        return snapshot

    def apply_fill(self, fill: Fill) -> None:
        """Apply a fill after marking its reference price."""

        self.mark(
            timestamp=fill.timestamp,
            reference_price_ticks=fill.reference_price_ticks,
            executable_bid_ticks=fill.executable_bid_ticks,
            executable_ask_ticks=fill.executable_ask_ticks,
        )
        wealth_before_fill = self.wealth
        notional = fill.price_ticks * fill.quantity * self.tick_value
        if fill.side is Side.BUY:
            self.cash -= notional + fill.fee
            self.inventory += fill.quantity
            edge = (fill.reference_price_ticks - fill.price_ticks) * fill.quantity * self.tick_value
        else:
            self.cash += notional - fill.fee
            self.inventory -= fill.quantity
            edge = (fill.price_ticks - fill.reference_price_ticks) * fill.quantity * self.tick_value

        self.pnl.fees += fill.fee
        self.pnl.executable_mark_adjustment += (
            (self.wealth - wealth_before_fill + fill.fee) - edge
        )
        if fill.is_liquidation:
            self.pnl.liquidation_pnl += edge
        else:
            self.pnl.spread_capture += edge
        self.peak_wealth = max(self.peak_wealth, self.wealth)

    def record_adverse_selection(self, fill: Fill, future_reference_price_ticks: float) -> float:
        """Record positive cost when the post-fill move goes against the fill."""

        move = (
            fill.side.inventory_delta
            * (future_reference_price_ticks - fill.reference_price_ticks)
            * fill.quantity
            * self.tick_value
        )
        cost = -move
        self.pnl.adverse_selection_cost += cost
        return cost

    def metrics(self) -> dict[str, float]:
        inventory_values = [snapshot.inventory for snapshot in self.snapshots]
        max_abs_inventory = max((abs(value) for value in inventory_values), default=0)
        mean_abs_inventory = (
            sum(abs(value) for value in inventory_values) / len(inventory_values)
            if inventory_values
            else 0.0
        )
        return {
            "final_pnl": self.total_pnl,
            "final_wealth": self.wealth,
            "final_inventory": float(self.inventory),
            "spread_capture": self.pnl.spread_capture,
            "inventory_mark_to_market": self.pnl.inventory_mark_to_market,
            "fees": self.pnl.fees,
            "adverse_selection_cost": self.pnl.adverse_selection_cost,
            "liquidation_pnl": self.pnl.liquidation_pnl,
            "executable_mark_adjustment": self.pnl.executable_mark_adjustment,
            "max_abs_inventory": float(max_abs_inventory),
            "mean_abs_inventory": mean_abs_inventory,
            "max_drawdown": max((s.drawdown for s in self.snapshots), default=0.0),
            "accounting_error": self.accounting_error,
        }

    def _inventory_mark_price(
        self,
        *,
        bid: float | None = None,
        ask: float | None = None,
    ) -> float:
        if self.reference_price_ticks is None and bid is None and ask is None:
            raise RuntimeError("ledger has not been marked")
        reference = self.reference_price_ticks or bid or ask
        effective_bid = bid or self.executable_bid_ticks or reference
        effective_ask = ask or self.executable_ask_ticks or reference
        if self.inventory > 0:
            return effective_bid
        if self.inventory < 0:
            return effective_ask
        return reference
