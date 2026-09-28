"""BT-003: performance metrics on hand-computed series and against independent computations."""

import math

import numpy as np
import pandas as pd
import pytest

from xq.backtest.metrics import (
    drawdown_metrics,
    expected_shortfall,
    path_max_drawdowns,
    return_metrics,
    trade_metrics,
)

P = 252


def test_return_metrics_by_hand() -> None:
    r = pd.Series([0.01, -0.02, 0.03, 0.00, -0.01])
    m = return_metrics(r, P)
    mean, std = 0.002, float(np.std(r, ddof=1))
    assert m["annual_return"] == pytest.approx(mean * P)
    assert m["annual_volatility"] == pytest.approx(std * math.sqrt(P))
    assert m["sharpe"] == pytest.approx(mean / std * math.sqrt(P))
    # downside deviation: sqrt(mean([0, 0.0004, 0, 0, 0.0001])) = 0.01
    assert m["sortino"] == pytest.approx(0.2 * math.sqrt(P))
    assert m["worst_day"] == -0.02
    assert m["days"] == 5


def test_expected_shortfall_takes_the_worst_tail() -> None:
    r = np.arange(-50, 50) / 1000  # 100 returns from -0.050 to 0.049
    assert expected_shortfall(r, 0.95) == pytest.approx(0.048)  # mean of the worst five
    assert expected_shortfall(r, 0.99) == pytest.approx(0.050)
    assert expected_shortfall(r[:10], 0.99) == pytest.approx(0.050)  # at least one observation
    assert math.isnan(expected_shortfall([], 0.95))


def test_drawdown_depth_and_duration_by_hand() -> None:
    m = drawdown_metrics(pd.Series([100.0, 110, 99, 105, 120, 90, 95, 100, 121]), 100.0)
    assert m["max_drawdown"] == pytest.approx(30 / 120)
    assert m["max_drawdown_usd"] == pytest.approx(30)
    assert m["max_drawdown_days"] == 3  # 90, 95, 100 below the 120 peak
    unrecovered = drawdown_metrics(pd.Series([100.0, 90, 80, 85]), 100.0)
    assert unrecovered["max_drawdown_days"] == 3


def test_the_starting_capital_is_the_first_peak() -> None:
    # C-24 (2): a loss on the first day is already a drawdown; before, the first day's equity
    # was the first peak and this path reported 2/99 over two days
    m = drawdown_metrics(pd.Series([99.0, 98.0, 97.0]), 100.0)
    assert m["max_drawdown"] == pytest.approx(3 / 100)
    assert m["max_drawdown_usd"] == pytest.approx(3)
    assert m["max_drawdown_days"] == 3
    gain = drawdown_metrics(pd.Series([101.0, 99.0, 102.0]), 100.0)  # the capital is no ceiling
    assert gain["max_drawdown"] == pytest.approx(2 / 101)
    assert gain["max_drawdown_days"] == 1
    rows = path_max_drawdowns(np.array([[0.99, 0.98, 0.97], [1.01, 0.99, 1.02]]))
    np.testing.assert_allclose(rows, [0.03, 2 / 101])
    with pytest.raises(ValueError, match="capital"):
        drawdown_metrics(pd.Series([1.0]), 0.0)


def test_trade_metrics_use_closed_trades_only() -> None:
    trades = pd.DataFrame(
        {"pnl": [100.0, -50.0, 30.0, -20.0, 10.0], "open": [False, False, False, False, True]}
    )
    m = trade_metrics(trades)
    assert (m["trade_count"], m["open_trades"]) == (4, 1)
    assert m["win_rate"] == 0.5
    assert m["avg_win"] == 65
    assert m["avg_loss"] == -35
    assert m["expectancy"] == 15
    assert m["profit_factor"] == pytest.approx(130 / 70)
    assert trade_metrics(trades.iloc[[0]])["profit_factor"] == math.inf
    empty = trade_metrics(pd.DataFrame({"pnl": [], "open": []}))
    assert empty["trade_count"] == 0
    assert math.isnan(empty["win_rate"])


def test_metrics_match_an_independent_pandas_computation() -> None:
    rng = np.random.default_rng(4)
    r = pd.Series(rng.normal(0.0004, 0.01, 1000))
    equity = 100_000 * (1 + r.cumsum())
    m = return_metrics(r, P)
    assert m["sharpe"] == pytest.approx(r.mean() / r.std() * np.sqrt(P))
    peak = equity.cummax().clip(lower=100_000)  # the capital is the first peak
    drawdown = drawdown_metrics(equity, 100_000)["max_drawdown"]
    assert drawdown == pytest.approx(((peak - equity) / peak).max())
    worst = r.nsmallest(50)
    assert m["cvar_95"] == pytest.approx(-worst.mean())


def test_undefined_statistics_are_nan() -> None:
    flat = return_metrics(pd.Series([0.0, 0.0, 0.0]), P)
    assert math.isnan(flat["sharpe"])
    assert math.isnan(flat["sortino"])
    assert math.isnan(return_metrics(pd.Series([], dtype=float), P)["annual_return"])
    assert math.isnan(drawdown_metrics(pd.Series([], dtype=float), 1.0)["max_drawdown"])
