"""Finite-horizon Avellaneda-Stoikov-style quote generator."""

from __future__ import annotations

import math

from lob_sim.agents.interface import AgentView, MarketObservation, RawQuote, StrategyConfig


class AvellanedaStoikovMarketMaker:
    """Inventory- and volatility-aware continuous quote model.

    The quote is deliberately returned in continuous tick coordinates. A
    shared risk layer performs tick rounding and passive/non-crossing checks.
    """

    name = "avellaneda_stoikov"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def compute_raw_quote(self, observation: MarketObservation, state: AgentView) -> RawQuote:
        reference = observation.reference_price(self.config.reference_mode)
        tau = max(self.config.horizon_seconds - observation.timestamp, 0.0)
        sigma = max(observation.volatility_ticks, self.config.volatility_floor_ticks)
        gamma = self.config.gamma
        k = self.config.fill_decay_k
        inventory = state.scaled_inventory

        reservation = reference - inventory * gamma * sigma**2 * tau
        if gamma <= 1e-12:
            execution_term = 1.0 / k
        else:
            execution_term = math.log1p(gamma / k) / gamma
        half_width = gamma * sigma**2 * tau / 2.0 + execution_term
        half_width = min(
            max(half_width, self.config.min_half_width_ticks),
            self.config.max_half_width_ticks,
        )
        return RawQuote(
            reservation_price_ticks=reservation,
            bid_price_ticks=reservation - half_width,
            ask_price_ticks=reservation + half_width,
            half_width_ticks=half_width,
            metadata={
                "reference_price_ticks": reference,
                "sigma_ticks": sigma,
                "tau_seconds": tau,
                "scaled_inventory": inventory,
                "execution_term": execution_term,
            },
        )

