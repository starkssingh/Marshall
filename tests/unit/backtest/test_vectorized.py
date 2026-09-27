"""BT-002: the vectorized screener on hand-computed cases — next-quote fills on the correct side,
costs, financing over the triple rollover, missed fills, market closures and P&L identities."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.backtest.costs import CostModel
from xq.backtest.vectorized import BacktestResult, run_vectorized
from xq.core.config import CostModelConfig, load_config
from xq.core.errors import NaiveTimestampError
from xq.data.calendar import MarketClock

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 1), date(2024, 3, 31))
CAPITAL = 100_000.0
S = pd.Timedelta(seconds=1)
# Simple, exact costs: 3.5 USD per lot per side, 0.5 bp slippage, 3.6 %/year financing both ways
# (1 bp of notional per night at act/360), Wednesday triple.
COSTS = CostModel(
    CostModelConfig(
        venue="test",
        provisional=True,
        latency_ms=1000,
        max_fill_delay_s=300,
        commission={"per_lot_per_side_usd": 3.5},  # type: ignore[arg-type]
        slippage={"fixed_bps": 0.5, "sigma_multiple": 0.0},  # type: ignore[arg-type]
        financing={"long_rate_annual_pct": 3.6, "short_rate_annual_pct": 3.6},  # type: ignore[arg-type]
    ),
    CFG.instrument("xauusd"),
    CFG.sessions_config(),
)


def at(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def quotes(*rows: tuple[str, float, float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": [at(t) for t, _, _ in rows],
            "bid": [b for _, b, _ in rows],
            "ask": [a for _, _, a in rows],
        }
    )


def positions(*rows: tuple[str, float]) -> pd.Series:
    return pd.Series([p for _, p in rows], index=pd.DatetimeIndex([at(t) for t, _ in rows]))


def screen(pos: pd.Series, q: pd.DataFrame, costs: CostModel = COSTS) -> BacktestResult:
    return run_vectorized(pos, q, costs, CLOCK, capital=CAPITAL)


def test_a_long_round_trip_matches_a_hand_computation() -> None:
    q = quotes(
        ("2024-03-12 14:00:00.5", 1999.8, 2000.0),  # before t + latency: not usable
        ("2024-03-12 14:00:02", 1999.9, 2000.1),  # the entry quote (mid 2000)
        ("2024-03-12 16:00:01.5", 2009.9, 2010.1),  # the exit quote
    )
    result = screen(positions(("2024-03-12 14:00", 1.0), ("2024-03-12 16:00", 0.0)), q)
    fills = result.fills
    assert fills["fill_time"].tolist() == [at("2024-03-12 14:00:02"), at("2024-03-12 16:00:01.5")]
    np.testing.assert_allclose(fills["lots"], [0.5, -0.5])  # 100,000 / (2000 * 100 oz)
    np.testing.assert_allclose(fills["price"], [2000.1 * 1.00005, 2009.9 * 0.99995])
    day = result.daily.loc[date(2024, 3, 12)]
    assert day["gross_pnl"] == pytest.approx(50 * (2010.0 - 2000.0))  # 50 oz at mid
    assert day["spread_cost"] == pytest.approx(2 * 50 * 0.1)
    assert day["slippage_cost"] == pytest.approx(50 * 0.5e-4 * (2000.1 + 2009.9))
    assert day["commission"] == pytest.approx(2 * 0.5 * 3.5)
    assert day["financing"] == 0.0
    expected = 50 * (2009.9 * 0.99995 - 2000.1 * 1.00005) - 3.5
    assert day["net_pnl"] == pytest.approx(expected)
    assert day["return"] == pytest.approx(expected / CAPITAL)
    assert day["position_lots"] == 0.0
    trade = result.trades.iloc[0]
    assert (trade["side"], trade["open"]) == (1.0, False)
    assert trade["pnl"] == pytest.approx(expected)


def test_a_short_held_over_the_triple_rollover_pays_four_nights() -> None:
    q = quotes(
        ("2024-03-12 14:00:02", 1999.9, 2000.1),  # Tuesday: sell 0.5 lots
        ("2024-03-12 20:59:00", 1999.9, 2000.1),  # before Tuesday's rollover (x1)
        ("2024-03-13 20:59:00", 1999.9, 2000.1),  # before Wednesday's rollover (x3)
        ("2024-03-14 14:00:02", 1999.9, 2000.1),  # Thursday: buy back
    )
    result = screen(positions(("2024-03-12 14:00", -1.0), ("2024-03-14 14:00", 0.0)), q)
    night = 0.5 * 100 * 2000.0 * 0.036 / 360  # 10 USD on 100,000 notional
    np.testing.assert_allclose(result.financing, [night, 3 * night])
    daily = result.daily
    assert daily.index.tolist() == [date(2024, 3, 12), date(2024, 3, 13), date(2024, 3, 14)]
    np.testing.assert_allclose(daily["financing"], [night, 3 * night, 0.0])
    np.testing.assert_allclose(daily["position_lots"], [-0.5, -0.5, 0.0])
    trade = result.trades.iloc[0]
    assert trade["side"] == -1.0
    assert trade["pnl"] == pytest.approx(daily["net_pnl"].sum())


def test_a_fill_later_than_the_allowed_delay_is_missed_and_retried() -> None:
    q = quotes(
        ("2024-03-12 14:10:00", 1999.9, 2000.1),  # 10 minutes after the first decision: too late
        ("2024-03-12 14:15:01", 1999.9, 2000.1),
    )
    result = screen(positions(("2024-03-12 14:00", 1.0), ("2024-03-12 14:15", 1.0)), q)
    assert result.missed.tolist() == [at("2024-03-12 14:00")]
    assert result.fills["decision_time"].tolist() == [at("2024-03-12 14:15")]


def test_a_decision_at_the_friday_close_fills_after_the_sunday_reopen() -> None:
    q = quotes(
        ("2024-03-15 20:59:59", 1999.9, 2000.1),  # the signal bar's last quote
        ("2024-03-17 22:00:03", 2004.9, 2005.1),  # Sunday reopen
    )
    result = screen(positions(("2024-03-15 21:00", 1.0)), q)
    assert result.fills["fill_time"].tolist() == [at("2024-03-17 22:00:03")]
    assert result.fills["price"].iloc[0] == pytest.approx(2005.1 * 1.00005)
    assert (result.fills["fill_time"] >= result.fills["decision_time"] + S).all()
    assert bool(result.trades["open"].iloc[0])


def test_a_side_flip_closes_one_trade_and_opens_another() -> None:
    q = quotes(
        ("2024-03-12 14:00:02", 1999.9, 2000.1),
        ("2024-03-12 15:00:02", 2009.9, 2010.1),
        ("2024-03-12 16:00:02", 1999.9, 2000.1),
    )
    result = screen(
        positions(("2024-03-12 14:00", 1.0), ("2024-03-12 15:00", -1.0), ("2024-03-12 16:00", 0.0)),
        q,
    )
    trades = result.trades
    assert trades["side"].tolist() == [1.0, -1.0]
    assert not trades["open"].any()
    assert trades["pnl"].sum() == pytest.approx(result.daily["net_pnl"].sum())
    assert trades["pnl"].iloc[0] > 0  # long from 2000 to 2010
    assert trades["pnl"].iloc[1] > 0  # short from 2010 to 2000


def test_pnl_identities_hold_for_random_positions() -> None:
    rng = np.random.default_rng(1)
    times = pd.date_range("2024-03-11 00:00", "2024-03-15 20:00", freq="15min", tz="UTC")
    decisions = times[rng.random(len(times)) < 0.3]
    targets = pd.Series(rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0], len(decisions)), index=decisions)
    targets.iloc[-1] = 0.0
    ticks = pd.date_range("2024-03-11 00:00", "2024-03-15 20:59", freq="37s", tz="UTC")
    mid = 2000 + np.cumsum(rng.normal(0, 0.2, len(ticks)))
    spread = rng.uniform(0.1, 0.4, len(ticks))
    q = pd.DataFrame({"ts_utc": ticks, "bid": mid - spread / 2, "ask": mid + spread / 2})
    result = screen(targets, q)
    daily = result.daily
    costs = daily[["spread_cost", "slippage_cost", "commission", "financing"]].sum(axis=1)
    np.testing.assert_allclose(daily["net_pnl"], daily["gross_pnl"] - costs, atol=1e-6)
    assert daily["equity"].iloc[-1] == pytest.approx(CAPITAL + daily["net_pnl"].sum())
    assert result.trades["pnl"].sum() == pytest.approx(daily["net_pnl"].sum())
    assert (daily["financing"] > 0).any()
    fills = result.fills
    buys = fills["lots"] > 0
    assert (fills.loc[buys, "price"] >= fills.loc[buys, "ask"]).all()  # never at mid
    assert (fills.loc[~buys, "price"] <= fills.loc[~buys, "bid"]).all()
    assert (fills["fill_time"] >= fills["decision_time"] + S).all()


def test_inputs_are_validated() -> None:
    q = quotes(("2024-03-12 14:00:02", 1999.9, 2000.1))
    with pytest.raises(NaiveTimestampError):
        screen(pd.Series([1.0], index=pd.DatetimeIndex(["2024-03-12 14:00"])), q)
    with pytest.raises(ValueError, match="missing"):
        screen(positions(("2024-03-12 14:00", float("nan"))), q)
    with pytest.raises(ValueError, match="increasing"):
        screen(positions(("2024-03-12 15:00", 1.0), ("2024-03-12 14:00", 0.0)), q)
    empty = screen(positions(("2024-03-12 14:00", 0.0)), q)
    assert empty.fills.empty
    assert empty.trades.empty
    assert (empty.daily["net_pnl"] == 0).all()
