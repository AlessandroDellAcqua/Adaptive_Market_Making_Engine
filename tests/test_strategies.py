from lob_sim.agents import (
    AgentView,
    AvellanedaStoikovMarketMaker,
    InventorySkewMarketMaker,
    MarketObservation,
    MicrostructureAdaptiveMarketMaker,
    NaiveSymmetricMarketMaker,
    RawQuote,
    StrategyConfig,
)
from lob_sim.risk import RiskLimits, sanitize_quote


def _observation(volatility: float = 1.0) -> MarketObservation:
    return MarketObservation(
        timestamp=1.0,
        best_bid_ticks=9_998,
        best_ask_ticks=10_002,
        bid_size=10,
        ask_size=10,
        volatility_ticks=volatility,
        fair_price_ticks=10_000,
    )


def test_positive_inventory_shifts_inventory_skew_downward() -> None:
    config = StrategyConfig(half_width_ticks=2.0, inventory_kappa=4.0)
    strategy = InventorySkewMarketMaker(config)
    flat = AgentView(1.0, 0, 1_000.0, 0, 10.0, 10)
    long = AgentView(1.0, 5, 1_000.0, 0, 10.0, 10)

    flat_quote = strategy.compute_raw_quote(_observation(), flat)
    long_quote = strategy.compute_raw_quote(_observation(), long)

    assert long_quote.reservation_price_ticks < flat_quote.reservation_price_ticks
    assert long_quote.bid_price_ticks < flat_quote.bid_price_ticks
    assert long_quote.ask_price_ticks < flat_quote.ask_price_ticks


def test_avellaneda_stoikov_width_increases_with_volatility() -> None:
    strategy = AvellanedaStoikovMarketMaker(
        StrategyConfig(gamma=0.2, fill_decay_k=0.5, horizon_seconds=10.0)
    )
    state = AgentView(1.0, 0, 1_000.0, 0, 10.0, 10)
    low = strategy.compute_raw_quote(_observation(0.5), state)
    high = strategy.compute_raw_quote(_observation(5.0), state)

    assert high.half_width_ticks > low.half_width_ticks


def test_risk_gate_accounts_for_live_order_exposure() -> None:
    state = AgentView(1.0, 0, 1_000.0, 0, 10.0, 10)
    raw = RawQuote(10_000.0, 9_998.0, 10_002.0, 2.0)
    action = sanitize_quote(
        raw,
        observation=_observation(),
        state=state,
        limits=RiskLimits(q_max=10, max_order_size=1),
        active_bid_qty=10,
        strategy_name="test",
    )

    assert action.bid_price_ticks is None
    assert action.ask_price_ticks is not None


def test_hard_inventory_boundary_only_allows_reducing_side() -> None:
    strategy = NaiveSymmetricMarketMaker(StrategyConfig())
    state = AgentView(1.0, 10, 1_000.0, 0, 10.0, 10)
    raw = strategy.compute_raw_quote(_observation(), state)
    action = sanitize_quote(
        raw,
        observation=_observation(),
        state=state,
        limits=RiskLimits(q_max=10, max_order_size=1),
        strategy_name=strategy.name,
    )

    assert action.bid_price_ticks is None
    assert action.ask_price_ticks is not None


def test_reduced_risk_state_widens_and_caps_quotes() -> None:
    state = AgentView(1.0, 8, 1_000.0, 0, 10.0, 10, risk_status="REDUCED")
    raw = RawQuote(
        reservation_price_ticks=10_000.0,
        bid_price_ticks=9_998.0,
        ask_price_ticks=10_002.0,
        half_width_ticks=2.0,
        bid_size=4,
        ask_size=4,
    )
    action = sanitize_quote(
        raw,
        observation=_observation(),
        state=state,
        limits=RiskLimits(
            q_max=10,
            max_order_size=4,
            reduced_order_size_factor=0.5,
            reduced_width_multiplier=1.5,
        ),
        strategy_name="test",
    )

    assert action.risk_status == "REDUCED"
    assert action.half_width_ticks == 3.0
    assert action.bid_size == 2
    assert action.ask_size == 2


def test_microstructure_adaptive_quote_uses_causal_flow_and_toxicity() -> None:
    strategy = MicrostructureAdaptiveMarketMaker(
        StrategyConfig(
            half_width_ticks=1.0,
            max_order_size=2,
            reference_mode="microprice",
            imbalance_kappa=4.0,
            flow_kappa=2.0,
            toxicity_width_kappa=3.0,
            toxicity_size_scale=1.0,
        )
    )
    state = AgentView(1.0, 0, 1_000.0, 0, 10.0, 10)
    balanced = _observation()
    toxic = MarketObservation(
        timestamp=1.0,
        best_bid_ticks=9_998,
        best_ask_ticks=10_002,
        bid_size=18,
        ask_size=2,
        volatility_ticks=1.0,
        signed_trade_flow=1.0,
        toxicity_score=1.0,
    )

    balanced_quote = strategy.compute_raw_quote(balanced, state)
    toxic_quote = strategy.compute_raw_quote(toxic, state)

    assert toxic_quote.reservation_price_ticks > balanced_quote.reservation_price_ticks
    assert toxic_quote.half_width_ticks > balanced_quote.half_width_ticks
    assert toxic_quote.bid_size is not None and toxic_quote.bid_size < 2
    assert toxic_quote.ask_size is not None and toxic_quote.ask_size < 2
