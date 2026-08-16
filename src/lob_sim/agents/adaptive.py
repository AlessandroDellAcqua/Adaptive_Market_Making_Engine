"""Microstructure-aware adaptive market-making policy."""

from __future__ import annotations

from lob_sim.agents.interface import AgentView, MarketObservation, RawQuote, StrategyConfig


class MicrostructureAdaptiveMarketMaker:
    """Use only current-book features to adapt fair value, width, and size.

    The policy is intentionally interpretable: imbalance and signed flow move
    the reservation price, while volatility and toxicity widen quotes. Inventory
    and toxicity reduce the side-specific requested size. The shared risk layer
    still owns final rounding, non-crossing, and hard inventory limits.
    """

    name = "microstructure_adaptive"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def compute_raw_quote(self, observation: MarketObservation, state: AgentView) -> RawQuote:
        reference = observation.reference_price(self.config.reference_mode)
        signal = (
            self.config.imbalance_kappa * observation.imbalance
            + self.config.flow_kappa * observation.signed_trade_flow
        )
        reservation = reference + signal - self.config.inventory_kappa * state.scaled_inventory
        excess_volatility = max(
            observation.volatility_ticks - self.config.volatility_floor_ticks,
            0.0,
        )
        half_width = (
            self.config.half_width_ticks
            + self.config.volatility_width_kappa * excess_volatility
            + self.config.toxicity_width_kappa * observation.toxicity_score
        )
        half_width = min(
            max(half_width, self.config.min_half_width_ticks),
            self.config.max_half_width_ticks,
        )

        base_size = self.config.max_order_size
        toxicity_factor = max(
            0.0,
            1.0 - self.config.toxicity_size_scale * observation.toxicity_score,
        )
        inventory_factor_bid = max(0.0, 1.0 - max(state.scaled_inventory, 0.0))
        inventory_factor_ask = max(0.0, 1.0 + min(state.scaled_inventory, 0.0))
        bid_size = round(base_size * toxicity_factor * inventory_factor_bid)
        ask_size = round(base_size * toxicity_factor * inventory_factor_ask)

        return RawQuote(
            reservation_price_ticks=reservation,
            bid_price_ticks=reservation - half_width,
            ask_price_ticks=reservation + half_width,
            half_width_ticks=half_width,
            bid_size=bid_size,
            ask_size=ask_size,
            metadata={
                "reference_price_ticks": reference,
                "imbalance": observation.imbalance,
                "signed_trade_flow": observation.signed_trade_flow,
                "cancellation_rate": observation.cancellation_rate,
                "queue_depletion_rate": observation.queue_depletion_rate,
                "toxicity_score": observation.toxicity_score,
                "scaled_inventory": state.scaled_inventory,
            },
        )
