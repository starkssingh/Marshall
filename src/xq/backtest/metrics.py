"""Performance metrics (BT-003).

Daily statistics are computed on trading-day returns ``r`` (net P&L over capital, BT-002) and
annualized with ``periods_per_year`` (``backtest.periods_per_year``, 252). The risk-free rate is
zero: financing is already charged in the P&L.

- ``annual_return`` = mean(r) * P; ``cagr`` = (final equity / capital)^(P / N) - 1;
- ``annual_volatility`` = std(r, ddof=1) * sqrt(P); ``sharpe`` = mean(r) / std(r) * sqrt(P);
- ``sortino`` = mean(r) / downside deviation * sqrt(P), where the downside deviation is
  sqrt(mean(min(r, 0)^2));
- ``max_drawdown``: the largest fall of equity from a running peak, as a fraction of that peak
  (and ``max_drawdown_usd``); ``max_drawdown_days``: the longest time, in trading days, from a
  peak until equity is back at it (or the end); ``calmar`` = annual_return / max_drawdown;
  ``recovery_factor`` = net profit / max_drawdown_usd;
- ``cvar_95`` / ``cvar_99``: the mean loss of the worst 5 % / 1 % of days (the worst
  ceil(N * (1 - q)) returns), as a positive fraction; ``worst_day`` = min(r);
- ``time_in_market``: share of days ending with a position; ``avg_exposure``: mean end-of-day
  gross exposure; ``turnover``: traded notional over capital per year;
- trades (closed holding episodes only; ``open_trades`` counts the rest): ``trade_count``,
  ``win_rate``, ``avg_win``, ``avg_loss`` (USD, a loss is negative), ``expectancy`` (mean trade
  P&L), ``profit_factor`` = gross wins / gross losses (infinite without losses).

A statistic that is undefined for the data (no variance, no trades) is NaN.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.backtest.vectorized import BacktestResult

FloatArray = npt.NDArray[np.float64]


def return_metrics(returns: pd.Series, periods_per_year: int) -> dict[str, float]:
    """Annualized return, volatility, Sharpe, Sortino, CVaR and worst day of daily returns."""
    r = returns.to_numpy(dtype=np.float64)
    n = len(r)
    mean = float(np.mean(r)) if n else math.nan
    std = float(np.std(r, ddof=1)) if n > 1 else math.nan
    downside = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2))) if n else math.nan
    root = math.sqrt(periods_per_year)
    return {
        "days": float(n),
        "annual_return": mean * periods_per_year,
        "annual_volatility": std * root,
        "sharpe": _ratio(mean, std) * root,
        "sortino": _ratio(mean, downside) * root,
        "cvar_95": expected_shortfall(r, 0.95),
        "cvar_99": expected_shortfall(r, 0.99),
        "worst_day": float(np.min(r)) if n else math.nan,
    }


def expected_shortfall(returns: npt.ArrayLike, level: float) -> float:
    """Mean loss of the worst ``ceil(N * (1 - level))`` returns, as a positive number."""
    r = np.sort(np.asarray(returns, dtype=np.float64))
    if len(r) == 0:
        return math.nan
    tail = max(1, math.ceil(len(r) * (1 - level) - 1e-9))
    return float(-np.mean(r[:tail]))


def drawdown_metrics(equity: pd.Series) -> dict[str, float]:
    """Maximum drawdown (fraction of the running peak and USD) and its longest duration in days."""
    values = equity.to_numpy(dtype=np.float64)
    if len(values) == 0:
        return {
            "max_drawdown": math.nan,
            "max_drawdown_usd": math.nan,
            "max_drawdown_days": math.nan,
        }
    peak = np.maximum.accumulate(values)
    fall = peak - values
    longest = run = 0
    for below in fall > 0:
        run = run + 1 if below else 0
        longest = max(longest, run)
    return {
        "max_drawdown": float(np.max(fall / peak)),
        "max_drawdown_usd": float(np.max(fall)),
        "max_drawdown_days": float(longest),
    }


def trade_metrics(trades: pd.DataFrame) -> dict[str, float]:
    """Win rate, average win and loss, expectancy and profit factor of closed trades."""
    closed = trades.loc[~trades["open"].astype(bool)] if len(trades) else trades
    pnl = closed["pnl"].to_numpy(dtype=np.float64) if len(closed) else np.array([])
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    return {
        "trade_count": float(len(pnl)),
        "open_trades": float(len(trades) - len(closed)),
        "win_rate": float(len(wins) / len(pnl)) if len(pnl) else math.nan,
        "avg_win": float(np.mean(wins)) if len(wins) else math.nan,
        "avg_loss": float(np.mean(losses)) if len(losses) else math.nan,
        "expectancy": float(np.mean(pnl)) if len(pnl) else math.nan,
        "profit_factor": _profit_factor(wins, losses),
    }


def performance_metrics(result: BacktestResult, periods_per_year: int) -> dict[str, float]:
    """Every BT-003 metric of one screened backtest (see the module docstring)."""
    daily = result.daily
    metrics = return_metrics(daily["return"], periods_per_year)
    metrics.update(drawdown_metrics(daily["equity"]))
    metrics.update(trade_metrics(result.trades))
    n = len(daily)
    capital = result.capital
    final = float(daily["equity"].iloc[-1]) if n else capital
    net_profit = final - capital
    metrics["net_profit"] = net_profit
    metrics["cagr"] = (
        (final / capital) ** (periods_per_year / n) - 1 if n and final > 0 else math.nan
    )
    metrics["calmar"] = _ratio(metrics["annual_return"], metrics["max_drawdown"])
    metrics["recovery_factor"] = _ratio(net_profit, metrics["max_drawdown_usd"])
    metrics["time_in_market"] = float((daily["position_lots"] != 0).mean()) if n else math.nan
    metrics["avg_exposure"] = float(daily["exposure"].mean()) if n else math.nan
    fills = result.fills
    traded = float((fills["lots"].abs() * fills["mid"]).sum()) * result.contract_size
    metrics["turnover"] = traded / capital * periods_per_year / n if n else math.nan
    metrics["gross_profit"] = float(daily["gross_pnl"].sum())
    metrics["total_costs"] = float(
        daily[["spread_cost", "slippage_cost", "commission", "financing"]].to_numpy().sum()
    )
    return metrics


def _ratio(numerator: float, denominator: float) -> float:
    if math.isnan(numerator) or math.isnan(denominator) or denominator == 0:
        return math.nan
    return numerator / denominator


def _profit_factor(wins: FloatArray, losses: FloatArray) -> float:
    if len(wins) == 0 and len(losses) == 0:
        return math.nan
    if len(losses) == 0:
        return math.inf
    return float(np.sum(wins) / -np.sum(losses))
