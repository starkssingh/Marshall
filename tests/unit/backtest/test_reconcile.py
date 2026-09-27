"""BT-009: vectorized versus event reconciliation on shared market-order strategies — within 5 % of
total costs, every difference explained, the mechanical residual zero, and planted disagreements
caught.

The event tier runs through the real risk engine (RISK-005). To compare execution mechanics, most
tests give it a profile in which the requested exposure binds — a 5 % risk budget to stops 4.5
daily sigma-hats away and a lot cap at the instrument's maximum, so the size is the strategy's
exposure as in the screener — and the halts never do; a sigma-hat of 1 % is known from the start,
so no entry is refused for want of one. The stops are far enough never to fill in these two weeks
(the screener has none). With the default profile the risk engine's sizes and refusals are
explained differences too (the last test).
"""

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CFG,
    CLOCK,
    RISK,
    SIGMA,
    ScriptedStrategy,
    exact_costs,
    random_quotes,
    risk_engine,
)
from xq.backtest.constraints import SessionConstraints
from xq.backtest.costs import CostModel
from xq.backtest.engine import EventBacktestResult, MarketData, run_event_backtest
from xq.backtest.reconcile import Reconciliation, reconcile
from xq.backtest.strategies import ExposureStrategy, RuleStrategy, signal_frame
from xq.core.config import CostModelConfig
from xq.core.types import Timeframe
from xq.models.baselines import RuleStrategyConfig, rule_exposure
from xq.risk.engine import RiskEngine
from xq.signals.schema import TradeIntent

TOLERANCE = CFG.backtest_config().event_config().reconcile_tolerance
PLACEHOLDER = CostModel.from_config(CFG, "xauusd")  # rollover and release slippage multipliers
#: The requested exposure binds (5 % of equity to a 4.5-sigma stop is more, and the lot cap is
#: the instrument's), no halt binds.
MECHANICS = risk_engine(
    risk_per_trade=0.05,
    max_lots=100,
    max_daily_loss=0.99,
    max_drawdown=0.99,
    max_consecutive_losses=1_000_000,
    max_trades_per_day=1_000_000,
)
STOP_SIGMAS = 4.5
RULES = {
    "ma_crossover": RuleStrategyConfig(rule="ma_crossover", params={"fast": 4, "slow": 16}),
    "momentum": RuleStrategyConfig(rule="time_series_momentum", params={"lookback": 8}),
    "zscore": RuleStrategyConfig(
        rule="zscore_reversion", params={"lookback": 20, "entry": 1.5, "exit": 0.25}
    ),
}


def two_weeks(seed: int = 21) -> pd.DataFrame:
    return random_quotes("2024-03-04 00:00", "2024-03-16 00:00", seed=seed, every_s=15, step=0.25)


def run_both(
    rule: RuleStrategyConfig,
    q: pd.DataFrame,
    *,
    costs: CostModel,
    capital: float = CAPITAL,
    constraints: SessionConstraints | None = None,
    sigma: pd.Series | None = None,
    event_costs: CostModel | None = None,
    risk: RiskEngine = MECHANICS,
) -> tuple[EventBacktestResult, Reconciliation]:
    data = MarketData.from_ticks(q, Timeframe.M15)
    positions = rule_exposure(signal_frame(data.bars), rule)
    event = run_event_backtest(
        RuleStrategy(rule, stop_sigmas=STOP_SIGMAS),
        data,
        event_costs or costs,
        CLOCK,
        capital=capital,
        margin_rate=0.05,
        risk=risk,
        constraints=constraints,
        sigma_1m_bps=sigma,
        sigma_daily=SIGMA,
    )
    result = reconcile(positions, q, event, costs, CLOCK, tolerance=TOLERANCE, sigma_1m_bps=sigma)
    return event, result


@pytest.mark.parametrize("name", sorted(RULES))
@pytest.mark.parametrize("capital", [CAPITAL, 10_000_000.0])
def test_shared_market_order_strategies_reconcile_within_tolerance(
    name: str, capital: float
) -> None:
    q = two_weeks()
    data = MarketData.from_ticks(q, Timeframe.M15)
    sigma = pd.Series(
        np.random.default_rng(1).uniform(1.0, 4.0, len(data.bars)),
        index=signal_frame(data.bars).index,
    )
    event, result = run_both(RULES[name], q, costs=PLACEHOLDER, capital=capital, sigma=sigma)
    assert result.passed
    assert result.within_tolerance
    assert result.max_abs_difference_after_sizing <= 0.05 * result.total_costs
    assert result.explained
    assert result.max_abs_residual < 0.01  # the mechanics agree to the cent on identical orders
    assert set(result.causes["cause"]) <= {"match", "sizing"}
    assert len(event.fills) > 20
    assert result.screener.financing.sum() > 0  # overnight holdings, the triple Wednesday too
    # the event strategy computed the rule bar by bar and took the same decisions
    np.testing.assert_array_equal(
        result.screener.fills["decision_time"], event.fills["decision_time"]
    )


def test_every_sizing_difference_is_itemized_and_accounts_for_the_gap() -> None:
    event, result = run_both(RULES["ma_crossover"], two_weeks(), costs=exact_costs())
    sizing = result.decisions.loc[result.decisions["cause"] == "sizing"]
    assert len(sizing) > 0
    # the positions differ by one lot step of rounding down at most, plus the drift of equity
    # from the capital: the risk engine sizes the exposure of equity, the screener of capital
    held_event = event.fills["position_lots"].abs().to_numpy()
    held_screen = result.screener.fills["position_lots"].abs().to_numpy()
    drift = float(np.max(np.abs(event.daily["equity"] / CAPITAL - 1)))
    assert (np.abs(held_screen - held_event) < 0.01 + held_screen * drift + 1e-3).all()
    daily = result.daily
    np.testing.assert_allclose(
        daily["difference"], daily["execution_effect"] + daily["residual"], atol=1e-9
    )
    np.testing.assert_allclose(
        daily["difference"], daily["sizing_effect"] + daily["difference_after_sizing"], atol=1e-9
    )
    np.testing.assert_allclose(daily["residual"], 0.0, atol=0.01)
    # only sizing differs here, so the sized screen is the event tier to the cent
    np.testing.assert_allclose(daily["difference_after_sizing"], 0.0, atol=0.01)
    assert result.max_abs_sizing_effect > 1.0


def test_missed_and_closed_decisions_match_across_tiers() -> None:
    q = two_weeks(seed=5)
    t = q["ts_utc"]
    # no quotes for 6 minutes after some bar ends: those decisions miss the 300 s fill delay
    holes = pd.date_range("2024-03-05 14:00", "2024-03-14 14:00", freq="1D", tz="UTC")
    for hole in holes:
        q = q.loc[(t < hole) | (t >= hole + pd.Timedelta(minutes=6))]
        t = q["ts_utc"]
    positions_rule = RuleStrategyConfig(rule="time_series_momentum", params={"lookback": 1})
    _, result = run_both(positions_rule, q.reset_index(drop=True), costs=exact_costs())
    decisions = result.decisions
    assert (decisions["screener"] == "missed").sum() > 0
    assert (decisions["screener"] == "closed").sum() > 0
    both_missed = decisions.loc[decisions["screener"] == "missed", "event"]
    assert (both_missed == "expired").all()
    closed = decisions.loc[decisions["screener"] == "closed", "event"]
    assert closed.str.startswith("refused: market closed").all()
    assert result.passed


def test_event_only_rules_are_explained_even_beyond_the_tolerance() -> None:
    rules = SessionConstraints.from_config(CFG, CLOCK)  # blackouts the screener does not have
    _, result = run_both(RULES["ma_crossover"], two_weeks(), costs=exact_costs(), constraints=rules)
    assert result.explained  # every difference has a cause and the mechanics still agree
    assert result.max_abs_residual < 0.01
    events = result.decisions.loc[result.decisions["cause"] == "event rule", "detail"]
    assert events.str.startswith("refused: entry blackout").any()
    # a refused entry changes the P&L far beyond 5 % of costs, even after the sizing effect
    assert result.max_abs_difference_after_sizing > 0.05 * result.total_costs
    assert not result.within_tolerance
    assert not result.passed


def test_a_mechanical_disagreement_is_not_explained() -> None:
    wrong = CostModel(
        CostModelConfig.model_validate(
            {**exact_costs().config.model_dump(), "commission": {"per_lot_per_side_usd": 4.0}}
        ),
        exact_costs().instrument,
        exact_costs().sessions,
    )
    _, result = run_both(RULES["ma_crossover"], two_weeks(), costs=exact_costs(), event_costs=wrong)
    assert set(result.causes["cause"]) <= {"match", "sizing"}  # same orders, same quotes
    assert result.max_abs_residual > 1.0  # but the commissions disagree
    assert not result.explained


def test_different_fill_timing_is_unexplained() -> None:
    slower = CostModel(
        CostModelConfig.model_validate({**exact_costs().config.model_dump(), "latency_ms": 20_000}),
        exact_costs().instrument,
        exact_costs().sessions,
    )
    _, result = run_both(
        RULES["ma_crossover"], two_weeks(), costs=exact_costs(), event_costs=slower
    )
    unexplained = result.decisions.loc[result.decisions["cause"] == "unexplained", "detail"]
    assert (unexplained == "the tiers filled on different quotes").sum() > 0
    assert not result.explained


@pytest.mark.parametrize("capital", [CAPITAL, 10_000_000.0])
def test_an_exposure_schedule_replays_the_screeners_positions(capital: float) -> None:
    q = two_weeks(seed=9)
    data = MarketData.from_ticks(q, Timeframe.M15)
    times = signal_frame(data.bars).index
    rng = np.random.default_rng(9)
    levels = rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0], len(times))
    positions = pd.Series(np.where(rng.random(len(times)) < 0.1, levels, np.nan), index=times)
    positions = positions.ffill().fillna(0.0)
    event = run_event_backtest(
        ExposureStrategy(positions, stop_sigmas=STOP_SIGMAS),
        data,
        exact_costs(),
        CLOCK,
        capital=capital,
        margin_rate=0.05,
        risk=MECHANICS,
        sigma_daily=SIGMA,
    )
    result = reconcile(positions, q, event, exact_costs(), CLOCK, tolerance=TOLERANCE)
    assert len(event.fills) > 10
    assert result.explained
    assert set(result.causes["cause"]) <= {"match", "sizing"}
    # the tolerance applies after the sizing effect (ADR 0050), so small accounts pass too
    assert result.passed
    np.testing.assert_allclose(result.daily["difference"], result.daily["sizing_effect"], atol=0.01)
    if capital == CAPITAL:
        # 0.5 of 100,000 USD is 0.25 lots, so one 0.01-lot rounding step is 4 % of the
        # position: the sizing effect alone exceeds 5 % of costs, the rest is within it
        assert result.max_abs_sizing_effect > 0.05 * result.total_costs


def test_only_market_orders_can_be_reconciled() -> None:
    q = two_weeks()
    known = q.loc[q["ts_utc"] <= pd.Timestamp("2024-03-05 14:00", tz="UTC")].iloc[-1]
    stop = float(known["bid"]) - 30.0
    script = {"2024-03-05 14:00": [TradeIntent(direction="long", exposure=1.0, stop=stop)]}
    event = run_event_backtest(
        ScriptedStrategy(script),
        MarketData.from_ticks(q, Timeframe.M15),
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
        risk=RISK,
        sigma_daily=SIGMA,
    )
    assert len(event.fills) > 0
    positions = pd.Series([1.0], index=pd.DatetimeIndex(["2024-03-05 14:00"], tz="UTC"))
    if (event.fills["order_type"] != "market").any():
        with pytest.raises(ValueError, match="market-order"):
            reconcile(positions, q, event, exact_costs(), CLOCK, tolerance=TOLERANCE)
    else:  # the stop never triggered: nothing but market orders to compare
        assert reconcile(positions, q, event, exact_costs(), CLOCK, tolerance=TOLERANCE).explained


def test_the_default_risk_profile_reconciles_as_sizing_and_risk_rules() -> None:
    event, result = run_both(RULES["ma_crossover"], two_weeks(), costs=exact_costs(), risk=RISK)
    assert result.explained  # the risk engine's sizes and refusals are all causes
    assert result.max_abs_residual < 0.01
    # 0.5 % of equity to a stop 4.5 sigma-hats (90 USD) away: 0.05 lots, not the exposure's 0.5
    assert (event.fills["position_lots"].abs() <= 0.06).all()
    decisions = result.decisions
    assert (decisions["cause"] == "sizing").sum() > 20
    rules = decisions.loc[decisions["cause"] == "event rule", "detail"]
    assert rules.str.startswith("rejected: cooldown").any()  # new exposure halted (RISK-003)
    assert rules.str.startswith("risk rule: cooldown").any()  # a flip cut to an exit (RISK-005)
