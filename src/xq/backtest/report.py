"""Backtest report with cost decomposition, for either tier (BT-010).

`build_backtest_report` turns a `BacktestResult` of the vectorized screener or an
`EventBacktestResult` of the event tier into a deterministic report (`xq.research.reports`):

- **Summary.** Which tier, the cost basis ("screening, placeholder costs" while the cost model is
  provisional) and, for the event tier, the risk engine and its profile version; the BT-003
  metrics; the **cost decomposition** — gross P&L (fills at the reference mids, no costs),
  spread, slippage, commission, financing, net P&L — and the **cost-fragility flag**: a strategy
  whose gross P&L is below 1.5 times its costs is cost-fragile (plan Phase 13, research
  validation); the **ambiguous-bar share** — the share of bars with an active bracket whose range
  reached both the stop and the target, and how they were resolved (the screener has no
  brackets: not applicable).
- **Equity and drawdown** per trading day, with the daily table.
- **Monthly returns**, compounded from daily returns, by year and calendar month of the trading
  day, with the year's total.
- **Trade distribution**: closed trades' net P&L quantiles, win rate, averages and profit factor,
  holding times, and a histogram.
- **Exposure by session**: on a five-minute grid of market-open time, the share of each session's
  time with a position and the mean absolute exposure (position x the latest fill's mid x
  contract / capital), for Tokyo, London, New York, their overlap and outside all sessions.
- **Ledger summary** (event tier): ledger rows by kind and reason, and the ledger's broken links
  (none when every order is backed by an approved risk decision).

`write_backtest` writes the report (and, for the event tier, the ledger as Parquet) under the
run's report directory, logs them as run artifacts and records the backtest in the ``backtests``
table with its metrics.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from xq.backtest.costs import CostModel
from xq.backtest.engine import EventBacktestResult
from xq.backtest.ledger import Ledger
from xq.backtest.metrics import performance_metrics
from xq.backtest.vectorized import BacktestResult
from xq.core.config import SessionsConfig
from xq.data.calendar import MarketClock
from xq.datasets.calendar_columns import calendar_columns
from xq.research.reports import ReportBuilder, new_figure
from xq.tracking import registry
from xq.tracking.runs import RunContext

REPORT_DIR = "backtests"
#: A strategy whose gross P&L is below this multiple of its costs is cost-fragile (plan Phase 13).
COST_FRAGILITY_MULTIPLE = 1.5
COST_COLUMNS = ("spread_cost", "slippage_cost", "commission", "financing")
_GRID = pd.Timedelta(minutes=5)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def tier_of(result: BacktestResult) -> str:
    """``event`` for an event-tier result, ``vectorized`` for the screener's."""
    return "event" if isinstance(result, EventBacktestResult) else "vectorized"


def cost_decomposition(result: BacktestResult) -> pd.DataFrame:
    """Gross P&L, each cost, total costs and net P&L (USD), and each as a share of gross."""
    daily = result.daily
    gross = float(daily["gross_pnl"].sum())
    costs = {name: float(daily[name].sum()) for name in COST_COLUMNS}
    total = sum(costs.values())
    net = float(daily["net_pnl"].sum())
    rows = [
        ("gross P&L (fills at mid, no costs)", gross),
        ("spread", -costs["spread_cost"]),
        ("slippage", -costs["slippage_cost"]),
        ("commission", -costs["commission"]),
        ("financing", -costs["financing"]),
        ("total costs", -total),
        ("net P&L", net),
    ]
    frame = pd.DataFrame(rows, columns=["item", "usd"])
    frame["share_of_gross"] = frame["usd"] / gross if gross else math.nan
    return frame


def is_cost_fragile(result: BacktestResult) -> bool:
    """True when the gross P&L is below 1.5 times the total costs (no edge counts as fragile)."""
    daily = result.daily
    gross = float(daily["gross_pnl"].sum())
    total = float(daily[list(COST_COLUMNS)].to_numpy().sum())
    return gross < COST_FRAGILITY_MULTIPLE * total


def monthly_returns(daily: pd.DataFrame) -> pd.DataFrame:
    """Compounded returns by year (rows) and calendar month of the trading day, plus the year."""
    if daily.empty:
        return pd.DataFrame(columns=[*_MONTHS, "full_year"])
    days = pd.DatetimeIndex(pd.to_datetime([str(d) for d in daily.index]))
    growth = pd.Series(1.0 + daily["return"].to_numpy(np.float64), index=days)
    years, months = days.year.to_numpy(), days.month.to_numpy()
    by_month = growth.groupby([years, months]).prod() - 1.0
    table = by_month.unstack().reindex(columns=range(1, 13))
    table.columns = pd.Index(_MONTHS)
    table["full_year"] = growth.groupby(years).prod() - 1.0
    table.index.name = "year"
    return table


def trade_distribution(trades: pd.DataFrame) -> pd.DataFrame:
    """Closed trades' net P&L and holding-time statistics as one ``statistic, value`` table."""
    closed = trades.loc[~trades["open"].astype(bool)] if len(trades) else trades
    pnl = closed["pnl"].to_numpy(np.float64) if len(closed) else np.array([])
    rows: list[tuple[str, float]] = [("closed trades", float(len(pnl)))]
    rows.append(("open trades", float(len(trades) - len(closed))))
    if len(pnl):
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        hours = (
            pd.to_datetime(closed["exit_time"], utc=True)
            - pd.to_datetime(closed["entry_time"], utc=True)
        ).dt.total_seconds() / 3600
        rows += [
            ("mean P&L (USD)", float(np.mean(pnl))),
            ("std P&L (USD)", float(np.std(pnl, ddof=1)) if len(pnl) > 1 else math.nan),
            *((f"P&L p{q} (USD)", float(np.percentile(pnl, q))) for q in (5, 25, 50, 75, 95)),
            ("win rate", float(len(wins) / len(pnl))),
            ("average win (USD)", float(np.mean(wins)) if len(wins) else math.nan),
            ("average loss (USD)", float(np.mean(losses)) if len(losses) else math.nan),
            (
                "profit factor",
                float(np.sum(wins) / -np.sum(losses)) if len(losses) else math.inf,
            ),
            ("median holding (hours)", float(np.median(hours))),
            ("longest holding (hours)", float(np.max(hours))),
        ]
    return pd.DataFrame(rows, columns=["statistic", "value"])


def exposure_by_session(result: BacktestResult, sessions: SessionsConfig) -> pd.DataFrame:
    """Time in market and mean absolute exposure per session (module docstring)."""
    columns = ["session", "share_of_market_time", "time_in_market", "mean_abs_exposure"]
    fills = result.fills
    if result.daily.empty:
        return pd.DataFrame(columns=columns)
    first, last = result.daily.index[0], result.daily.index[-1]
    clock = MarketClock.for_range(sessions, first, last)
    start = pd.Timestamp(clock.covered_from, tz="UTC")
    end = pd.Timestamp(clock.covered_to, tz="UTC")
    grid = pd.date_range(start, end, freq=_GRID, inclusive="left")
    t = grid.as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    grid = grid[clock.is_open(t)]
    t = t[clock.is_open(t)]
    exposure = np.zeros(len(t))
    if len(fills):
        fill_t = pd.DatetimeIndex(fills["fill_time"]).as_unit("ns").to_numpy("datetime64[ns]")
        k = np.searchsorted(fill_t.view(np.int64), t, side="right") - 1
        held = k >= 0
        lots = fills["position_lots"].to_numpy(np.float64)
        mids = fills["mid"].to_numpy(np.float64)
        exposure[held] = np.abs(lots[k[held]] * mids[k[held]]) * result.contract_size
        exposure /= result.capital
    calendar = calendar_columns(grid, sessions)
    names = [*sessions.sessions, *sessions.overlaps]
    masks = {name: calendar[f"in_{name}"].to_numpy(bool) for name in names}
    outside = (
        ~np.any(np.column_stack(list(masks.values())), axis=1) if masks else np.ones(len(t), bool)
    )
    masks["outside sessions"] = outside
    masks["all market hours"] = np.ones(len(t), dtype=bool)
    rows = []
    for name, mask in masks.items():
        n = int(mask.sum())
        rows.append(
            {
                "session": name,
                "share_of_market_time": n / len(t) if len(t) else math.nan,
                "time_in_market": float((exposure[mask] > 0).mean()) if n else math.nan,
                "mean_abs_exposure": float(exposure[mask].mean()) if n else math.nan,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def build_backtest_report(
    result: BacktestResult,
    *,
    title: str,
    sessions: SessionsConfig,
    periods_per_year: int,
    metadata: Mapping[str, Any] | None = None,
) -> ReportBuilder:
    """The report of one backtest (module docstring); `ReportBuilder.build` writes it."""
    tier = tier_of(result)
    event = result if isinstance(result, EventBacktestResult) else None
    info: dict[str, Any] = {
        "tier": tier,
        "cost_basis": result.cost_basis,
        "capital_usd": result.capital,
        "first_day": str(result.daily.index[0]) if len(result.daily) else None,
        "last_day": str(result.daily.index[-1]) if len(result.daily) else None,
        **(dict(metadata) if metadata else {}),
    }
    if event is not None:
        info.update(
            risk_engine=event.risk_label,
            data_mode=event.mode,
            strategy=f"{event.strategy_id} v{event.strategy_version}",
        )
    builder = ReportBuilder(title, metadata=info)
    metrics = performance_metrics(result, periods_per_year)
    fragile = is_cost_fragile(result)

    summary = builder.section("summary", "Summary")
    lines = [
        f"- **Tier:** {tier}"
        + (f" ({event.mode} mode)" if event is not None else " (research screener)"),
        f"- **Net results:** {result.cost_basis}",
        "- **Risk engine:** "
        + (
            event.risk_label
            if event is not None
            else "none (the screener's exposures are positions)"
        ),
        f"- **Cost-fragile:** {'yes' if fragile else 'no'} (gross P&L "
        f"{'below' if fragile else 'at least'} {COST_FRAGILITY_MULTIPLE}x total costs)",
        "- **Ambiguous bars:** " + _ambiguity_line(event),
    ]
    if event is not None and event.link_problems:
        lines.append(f"- **Ledger:** {len(event.link_problems)} broken links (see the ledger)")
    summary.text("\n".join(lines))
    summary.table(
        "cost_decomposition",
        cost_decomposition(result),
        caption=f"Gross to net P&L, USD ({result.cost_basis})",
        digits=6,
    )
    summary.table(
        "metrics",
        pd.DataFrame(sorted(metrics.items()), columns=["metric", "value"]),
        caption=f"BT-003 metrics on daily returns ({result.cost_basis})",
    )

    equity = builder.section("equity", "Equity and drawdown")
    equity.figure("equity", _equity_figure(result), caption="Equity and drawdown per trading day")
    equity.table("daily", result.daily.reset_index(), caption="Daily P&L", max_rows=0)

    monthly = builder.section("monthly", "Monthly returns")
    monthly.table(
        "monthly_returns",
        monthly_returns(result.daily).reset_index(),
        caption=f"Compounded returns by month of the trading day ({result.cost_basis})",
    )

    trades = builder.section("trades", "Trade distribution")
    trades.table(
        "distribution", trade_distribution(result.trades), caption="Closed trades (net P&L)"
    )
    trades.figure("pnl_histogram", _trade_histogram(result), caption="Net P&L per closed trade")

    exposure = builder.section("exposure", "Exposure by session")
    exposure.table(
        "by_session",
        exposure_by_session(result, sessions),
        caption="Share of market time, time in market and mean absolute exposure per session",
    )

    if event is not None:
        ledger = builder.section("ledger", "Decision ledger")
        ledger.text(
            "Broken links: none."
            if not event.link_problems
            else "Broken links:\n\n" + "\n".join(f"- {p}" for p in event.link_problems)
        )
        ledger.table("summary", event.ledger_summary, caption="Ledger rows by kind and reason")
    return builder


def cost_model_version(costs: CostModel) -> str:
    """The cost model's venue and a hash of its configuration."""
    payload = json.dumps(costs.config.model_dump(mode="json"), sort_keys=True)
    return f"{costs.config.venue}@{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def write_backtest(
    run: RunContext,
    result: BacktestResult,
    costs: CostModel,
    *,
    title: str,
    strategy_id: str,
    strategy_version: str,
    name: str,
) -> registry.BacktestRef:
    """Write the report (and the event tier's ledger), log them and record the backtest.

    Files go to ``<reports_dir>/backtests/<run_id>/<name>/``.
    """
    cfg = run.cfg
    backtest = cfg.backtest_config()
    directory = cfg.paths.resolve(cfg.paths.reports_dir) / REPORT_DIR / run.run_id / name
    builder = build_backtest_report(
        result,
        title=title,
        sessions=cfg.sessions_config(),
        periods_per_year=backtest.periods_per_year,
        metadata={"run_id": run.run_id, "cost_model": cost_model_version(costs)},
    )
    builder.build(directory)
    run.log_artifact(directory / "summary.md", kind="backtest_report")
    ledger_path: str | None = None
    if isinstance(result, EventBacktestResult):
        paths = _write_ledger(result, directory)
        run.log_artifact(paths["ledger"], kind="backtest_ledger")
        ledger_path = str(paths["ledger"])
    metrics = performance_metrics(result, backtest.periods_per_year)
    if isinstance(result, EventBacktestResult):
        metrics["ambiguous_share"] = float(result.ambiguity["ambiguous_share"])
    days = result.daily.index
    return registry.add_backtest(
        run.engine,
        run.run_id,
        tier=tier_of(result),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        cost_model_version=cost_model_version(costs),
        start=pd.Timestamp(str(days[0]), tz="UTC"),
        end=pd.Timestamp(str(days[-1]), tz="UTC"),
        metrics=metrics,
        ledger_path=ledger_path,
        report_path=str(directory),
    )


def _write_ledger(result: EventBacktestResult, directory: Path) -> dict[str, Path]:
    ledger = Ledger()
    ledger.rows = [
        {**{str(k): v for k, v in row.items()}, "ts": pd.Timestamp(row["ts"]).value}
        for row in result.ledger.to_dict("records")
    ]
    return ledger.write(directory)


def _ambiguity_line(event: EventBacktestResult | None) -> str:
    if event is None:
        return "not applicable (market orders only, no intrabar resolution)"
    a = event.ambiguity
    share = a["ambiguous_share"]
    shown = "n/a" if isinstance(share, float) and math.isnan(share) else f"{float(share):.2%}"
    return (
        f"{shown} of {int(float(a['bracket_bars']))} bars with an active bracket touched both "
        f"legs ({int(float(a['ambiguous_bars']))}); resolution: {a['resolution']}"
    )


def _equity_figure(result: BacktestResult) -> Any:
    figure = new_figure(8.0, 5.0)
    top, bottom = figure.subplots(2, 1, sharex=True, height_ratios=[2, 1])
    daily = result.daily
    x = pd.to_datetime(pd.Series(daily.index, dtype=object).astype(str))
    equity = daily["equity"].to_numpy(np.float64)
    peak = np.maximum.accumulate(equity) if len(equity) else equity
    top.plot(x, equity, color="#2a6f97", linewidth=1.2)
    top.set_ylabel("equity (USD)")
    top.set_title(f"Equity ({result.cost_basis})", fontsize=10)
    bottom.fill_between(x, (equity - peak) / peak if len(peak) else [], 0, color="#c44536")
    bottom.set_ylabel("drawdown")
    return figure


def _trade_histogram(result: BacktestResult) -> Any:
    figure = new_figure(8.0, 3.5)
    axis = figure.subplots()
    trades = result.trades
    closed = trades.loc[~trades["open"].astype(bool)] if len(trades) else trades
    pnl = closed["pnl"].to_numpy(np.float64) if len(closed) else np.array([])
    if len(pnl):
        axis.hist(pnl, bins=min(40, max(5, len(pnl) // 3)), color="#2a6f97")
    axis.axvline(0.0, color="black", linewidth=0.8)
    axis.set_xlabel("net P&L per closed trade (USD)")
    axis.set_ylabel("trades")
    return figure
