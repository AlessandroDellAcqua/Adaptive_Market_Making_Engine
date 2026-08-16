"""Simple market-making baselines used as scientific controls."""

from __future__ import annotations

from lob_sim.agents.interface import AgentView, MarketObservation, RawQuote, StrategyConfig


class NaiveSymmetricMarketMaker:
    """Fixed symmetric quotes with no inventory response."""

    name = "naive_symmetric"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def compute_raw_quote(self, observation: MarketObservation, state: AgentView) -> RawQuote:
        del state
        reference = observation.reference_price(self.config.reference_mode)
        half_width = min(
            max(self.config.half_width_ticks, self.config.min_half_width_ticks),
            self.config.max_half_width_ticks,
        )
        return RawQuote(
            reservation_price_ticks=reference,
            bid_price_ticks=reference - half_width,
            ask_price_ticks=reference + half_width,
            half_width_ticks=half_width,
            metadata={"reference_price_ticks": reference},
        )


class InventorySkewMarketMaker:
    """Fixed-width strategy that shifts both quotes against inventory."""

    name = "inventory_skew"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def compute_raw_quote(self, observation: MarketObservation, state: AgentView) -> RawQuote:
        reference = observation.reference_price(self.config.reference_mode)
        reservation = reference - self.config.inventory_kappa * state.scaled_inventory
        half_width = min(
            max(self.config.half_width_ticks, self.config.min_half_width_ticks),
            self.config.max_half_width_ticks,
        )
        return RawQuote(
            reservation_price_ticks=reservation,
            bid_price_ticks=reservation - half_width,
            ask_price_ticks=reservation + half_width,
            half_width_ticks=half_width,
            metadata={
                "reference_price_ticks": reference,
                "scaled_inventory": state.scaled_inventory,
            },
        )

