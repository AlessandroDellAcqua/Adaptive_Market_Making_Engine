from hypothesis import given
from hypothesis import strategies as st

from lob_sim.agents import AgentView, MarketObservation, RawQuote
from lob_sim.risk import RiskLimits, sanitize_quote


@given(
    inventory=st.integers(min_value=-10, max_value=10),
    bid=st.integers(min_value=9_000, max_value=11_000),
    ask=st.integers(min_value=9_000, max_value=11_000),
)
def test_sanitized_quotes_never_cross_or_breach_inventory(
    inventory: int, bid: int, ask: int
) -> None:
    observation = MarketObservation(
        timestamp=0.0,
        best_bid_ticks=9_998,
        best_ask_ticks=10_002,
        bid_size=10,
        ask_size=10,
        volatility_ticks=1.0,
    )
    state = AgentView(0.0, inventory, 1_000.0, 0, 10.0, 10)
    action = sanitize_quote(
        RawQuote(10_000.0, float(bid), float(ask), 2.0),
        observation=observation,
        state=state,
        limits=RiskLimits(q_max=10, max_order_size=1),
        strategy_name="property",
    )

    if action.bid_price_ticks is not None and action.ask_price_ticks is not None:
        assert action.bid_price_ticks < action.ask_price_ticks
    if action.bid_price_ticks is not None:
        assert inventory + action.bid_size <= 10
    if action.ask_price_ticks is not None:
        assert inventory - action.ask_size >= -10
