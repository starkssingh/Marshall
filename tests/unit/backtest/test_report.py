"""BT-010: the backtest report for both tiers — cost decomposition and the cost-fragility flag,
monthly returns, trade distribution, exposure by session, the ambiguous-bar share, determinism."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CLOCK,
    SESSIONS,
    RandomStrategy,
    exact_costs,
    random_quotes,
)
from xq.backtest.engine import EventBacktestResult, MarketData, run_event_backtest
from xq.backtest.metrics import performance_metrics
from xq.backtest.report import (
    build_backtest_report,
    cost_decomposition,
    exposure_by_session,
    is_cost_fragile,
    monthly_returns,
    trade_distribution,
)
from xq.backtest.strategies import RuleStrategy, signal_frame
from xq.backtest.vectorized import BacktestResult, run_vectorized
from xq.core.types import Timeframe
from xq.models.baselines import RuleStrategyConfig, rule_exposure

RULE = RuleStrategyConfig(rule="ma_crossover", params={"fast": 4, "slow": 16})


@pytest.fixture(scope="module")
def tiers() -> tuple[BacktestResult, EventBacktestResult]:
    """The same baseline rule through the screener and the event tier (two weeks, synthetic)."""
    q = random_quotes("2024-03-04 00:00", "2024-03-16 00:00", seed=21, every_s=15, step=0.25)
    data = MarketData.from_ticks(q, Timeframe.M15)
    positions = rule_exposure(signal_frame(data.bars), RULE)
    screener = run_vectorized(positions, q, exact_costs(), CLOCK, capital=CAPITAL)
    event = run_event_backtest(
        RuleStrategy(RULE), data, exact_costs(), CLOCK, capital=CAPITAL, margin_rate=0.05
    )
    return screener, event


def build(result: BacktestResult, directory: Path) -> dict[str, str]:
    report = build_backtest_report(
        result, title="MA crossover (synthetic)", sessions=SESSIONS, periods_per_year=252
    )
    return report.build(directory)


def test_baseline_reports_are_produced_by_both_tiers(
    tiers: tuple[BacktestResult, EventBacktestResult], tmp_path: Path
) -> None:
    screener, event = tiers
    for name, result in (("screener", screener), ("event", event)):
        files = build(result, tmp_path / name)
        for section in ("summary", "equity", "monthly", "trades", "exposure"):
            assert f"{section}.md" in files
        assert "figures/equity-equity.png" in files
        summary = (tmp_path / name / "summary.md").read_text()
        assert "screening, placeholder costs" in summary  # every net figure is marked
        assert "Cost-fragile:" in summary
        assert "Ambiguous bars:" in summary
    event_summary = (tmp_path / "event" / "summary.md").read_text()
    assert "PLACEHOLDER pass-through risk approver" in event_summary
    assert "ledger.md" in build(event, tmp_path / "event-again")
    assert "Broken links: none." in (tmp_path / "event" / "ledger.md").read_text()
    screener_summary = (tmp_path / "screener" / "summary.md").read_text()
    assert "not applicable (market orders only" in screener_summary
    assert "ledger.md" not in build(screener, tmp_path / "screener-again")


def test_the_report_is_deterministic(
    tiers: tuple[BacktestResult, EventBacktestResult], tmp_path: Path
) -> None:
    _, event = tiers
    assert build(event, tmp_path / "a") == build(event, tmp_path / "b")


def test_the_cost_decomposition_adds_up_to_net(
    tiers: tuple[BacktestResult, EventBacktestResult],
) -> None:
    for result in tiers:
        table = cost_decomposition(result).set_index("item")["usd"]
        daily = result.daily
        assert table["gross P&L (fills at mid, no costs)"] == pytest.approx(
            daily["gross_pnl"].sum()
        )
        assert table["financing"] == pytest.approx(-daily["financing"].sum())
        parts = ["spread", "slippage", "commission", "financing"]
        assert table[parts].sum() == pytest.approx(table["total costs"])
        assert table["gross P&L (fills at mid, no costs)"] + table["total costs"] == pytest.approx(
            table["net P&L"]
        )
        assert table["net P&L"] == pytest.approx(performance_metrics(result, 252)["net_profit"])


def test_cost_fragility_is_gross_below_one_and_a_half_times_costs() -> None:
    # a steady rise held long: gross far above costs; the same rise traded every bar: fragile
    times = pd.date_range("2024-03-11 14:00", "2024-03-14 20:00", freq="30s", tz="UTC")
    t = times.as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    times = times[CLOCK.is_open(t)]
    mid = np.linspace(2000.0, 2100.0, len(times))
    q = pd.DataFrame({"ts_utc": times, "bid": mid - 0.1, "ask": mid + 0.1})
    held = pd.Series(
        [1.0, 0.0], index=pd.DatetimeIndex(["2024-03-11 14:00", "2024-03-14 19:00"], tz="UTC")
    )
    trend = run_vectorized(held, q, exact_costs(), CLOCK, capital=CAPITAL)
    assert not is_cost_fragile(trend)
    decisions = pd.date_range("2024-03-12 14:00", "2024-03-12 18:00", freq="1min", tz="UTC")
    churn = pd.Series(
        np.tile([1.0, -1.0], len(decisions) // 2), index=decisions[: len(decisions) // 2 * 2]
    )
    churned = run_vectorized(churn, q, exact_costs(), CLOCK, capital=CAPITAL)
    assert is_cost_fragile(churned)


def test_monthly_returns_compound_the_daily_returns() -> None:
    days = pd.Index(pd.date_range("2024-01-29", "2024-03-05", freq="B").date, name="trading_day")
    daily = pd.DataFrame(
        {"return": np.random.default_rng(2).normal(0, 0.01, len(days))}, index=days
    )
    table = monthly_returns(daily)
    february = [d.month == 2 for d in days]
    assert table.loc[2024, "Feb"] == pytest.approx(np.prod(1 + daily["return"][february]) - 1)
    assert table.loc[2024, "full_year"] == pytest.approx(np.prod(1 + daily["return"]) - 1)
    assert np.isnan(table.loc[2024, "Dec"])


def test_trade_distribution_and_exposure_by_session(
    tiers: tuple[BacktestResult, EventBacktestResult],
) -> None:
    screener, event = tiers
    for result in (screener, event):
        stats = trade_distribution(result.trades).set_index("statistic")["value"]
        metrics = performance_metrics(result, 252)
        assert stats["closed trades"] == metrics["trade_count"]
        assert stats["win rate"] == pytest.approx(metrics["win_rate"])
        exposure = exposure_by_session(result, SESSIONS).set_index("session")
        assert set(exposure.index) >= {"tokyo", "london", "new_york", "outside sessions"}
        assert exposure.loc["all market hours", "share_of_market_time"] == 1.0
        assert 0 < exposure.loc["all market hours", "time_in_market"] <= 1
        assert (exposure["mean_abs_exposure"] < 1.05).all()  # about one unit of exposure at most
    # the same decisions, so the same exposure profile up to lot rounding
    a = exposure_by_session(screener, SESSIONS)["time_in_market"].to_numpy()
    b = exposure_by_session(event, SESSIONS)["time_in_market"].to_numpy()
    np.testing.assert_allclose(a, b, atol=0.02)


def test_the_ambiguous_bar_share_is_reported_in_bar_mode(tmp_path: Path) -> None:
    q = random_quotes("2024-03-11 00:00", "2024-03-16 00:00", seed=3, every_s=10, step=1.6)
    ticks = MarketData.from_ticks(q, Timeframe.M15)
    bars = MarketData.from_bars(ticks.bars, Timeframe.M15, execution_bars=ticks.minute_bars)
    strategy = RandomStrategy(seed=3, trade_probability=0.3)
    result = run_event_backtest(
        strategy, bars, exact_costs(), CLOCK, capital=CAPITAL, margin_rate=0.05
    )
    a = result.ambiguity
    assert a["resolution"] == "pessimistic: the stop loss is assumed first"
    assert a["ambiguous_bars"] > 0
    assert a["ambiguous_share"] == pytest.approx(a["ambiguous_bars"] / a["bracket_bars"])
    assert result.brackets["ambiguous"].sum() == a["ambiguous_bars"]
    ambiguous_exits = result.brackets.loc[result.brackets["ambiguous"], "exit_role"]
    assert (ambiguous_exits == "stop_loss").all()
    build(result, tmp_path)
    summary = (tmp_path / "summary.md").read_text()
    assert f"{a['ambiguous_share']:.2%}" in summary
    assert "pessimistic" in summary
    tick_result = run_event_backtest(
        RandomStrategy(seed=3, trade_probability=0.3),
        ticks,
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
    )
    assert tick_result.ambiguity["resolution"] == "ticks"
    assert tick_result.ambiguity["bracket_bars"] > 0
    assert tick_result.ambiguity["ambiguous_bars"] > 0  # bars alone could not have told
