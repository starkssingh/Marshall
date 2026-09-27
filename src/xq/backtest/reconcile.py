"""Reconciliation of the vectorized screener with the event tier (BT-009).

A market-order strategy — the same target exposures at the same decision times — is run through
both tiers (`run_vectorized` and `run_event_backtest`, typically with `ExposureStrategy` or
`RuleStrategy`), and `reconcile` compares them:

1. **Equity.** The daily equity of the event tier minus the screener's. The tiers agree within
   tolerance when the largest absolute daily difference is at most ``tolerance`` (default 5 %,
   ``backtest.event.reconcile_tolerance``) of the screener's total costs.
2. **Every difference explained.** The difference is split in two by replaying the event tier's
   *executed* positions through the screener (the *adjusted* screen): each event fill becomes the
   exposure that makes the screener trade exactly its lots at the same quote.

   - ``execution_effect`` = adjusted - screener: what the event tier executed differently. Each
     decision is classified (``decisions``) and every difference gets a cause: sizing (the event
     tier sizes at the decision's mid and rounds down to the lot step through the risk approver;
     the screener sizes fractional lots at the fill's mid), an event-tier rule the screener does
     not have (an entry blackout, a margin rejection, a weekend exit) or a follow-on of an earlier
     difference (the tiers then hold different positions).
   - ``residual`` = event - adjusted: what the fill, cost, financing and accounting mechanics
     disagree on for identical orders. It must be zero up to rounding
     (``residual_tolerance_usd``, one cent by default).

   The comparison is *explained* when no decision is left without a cause and the residual is
   within its tolerance. `Reconciliation.passed` requires both that and the tolerance of step 1.

Only market orders can be reconciled: the screener has no stops, targets or resting orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from xq.backtest.costs import CostModel
from xq.backtest.engine import EventBacktestResult
from xq.backtest.vectorized import BacktestResult, run_vectorized
from xq.data.calendar import MarketClock

COST_COLUMNS = ["spread_cost", "slippage_cost", "commission", "financing"]
#: Causes that explain a difference, with what they mean.
CAUSES = {
    "match": "both tiers executed the decision identically (or both skipped it)",
    "sizing": (
        "same fill quote, different lots: the event tier sizes at the mid known at the decision "
        "and rounds down to the lot step (risk approver); the screener sizes fractional lots at "
        "the fill's mid"
    ),
    "target unchanged after rounding": (
        "the event tier's rounded target equals its position, so no order was needed"
    ),
    "event rule": "an event-tier rule the screener does not have (see the detail)",
    "follow-on": "the tiers held different positions after an earlier explained difference",
    "unexplained": "no known cause: a mechanical disagreement to investigate",
}


@dataclass(frozen=True)
class Reconciliation:
    """The comparison of one strategy through both tiers (module docstring)."""

    daily: pd.DataFrame
    decisions: pd.DataFrame
    causes: pd.DataFrame
    total_costs: float
    tolerance: float
    tolerance_usd: float
    max_abs_difference: float
    max_abs_residual: float
    residual_tolerance_usd: float
    within_tolerance: bool
    explained: bool
    screener: BacktestResult
    adjusted: BacktestResult

    @property
    def passed(self) -> bool:
        """Within tolerance and every difference explained."""
        return self.within_tolerance and self.explained


def reconcile(
    positions: pd.Series,
    quotes: pd.DataFrame,
    event: EventBacktestResult,
    costs: CostModel,
    clock: MarketClock,
    *,
    tolerance: float,
    sigma_1m_bps: pd.Series | None = None,
    residual_tolerance_usd: float = 0.01,
) -> Reconciliation:
    """Compare `event` with the screener on the same `positions` (module docstring).

    Args:
        positions: The target exposures the event strategy followed, by decision time.
        quotes: The quotes both tiers executed on (``ts_utc``, ``bid``, ``ask``).
        event: The event tier's result for the same strategy.
        costs: The cost model both tiers used.
        clock: The market clock both tiers used.
        tolerance: Largest daily equity difference, as a share of the screener's total costs.
        sigma_1m_bps: Sigma-hat for slippage, as given to both tiers.
        residual_tolerance_usd: Largest mechanical residual counted as rounding.

    Raises:
        ValueError: if the event tier executed anything but market orders.
    """
    fills = event.fills
    if (fills["order_type"] != "market").any():
        raise ValueError("only market-order strategies can be reconciled (no stops or limits)")
    capital = event.capital
    quotes = _as_timestamps(quotes)
    screener = run_vectorized(
        positions, quotes, costs, clock, capital=capital, sigma_1m_bps=sigma_1m_bps
    )
    executed = pd.Series(
        (fills["position_lots"] * fills["mid"] * event.contract_size / capital).to_numpy(),
        index=pd.DatetimeIndex(fills["decision_time"]),
    )
    adjusted = run_vectorized(
        executed, quotes, costs, clock, capital=capital, sigma_1m_bps=sigma_1m_bps
    )
    daily = _daily(screener, adjusted, event, capital)
    decisions = _decisions(positions, screener, event)
    causes = (
        decisions.groupby("cause", sort=False)
        .size()
        .rename("decisions")
        .reset_index()
        .assign(explanation=lambda f: f["cause"].map(CAUSES))
    )
    total_costs = float(screener.daily[COST_COLUMNS].to_numpy().sum())
    max_difference = float(daily["difference"].abs().max()) if len(daily) else 0.0
    max_residual = float(daily["residual"].abs().max()) if len(daily) else 0.0
    return Reconciliation(
        daily=daily,
        decisions=decisions,
        causes=causes,
        total_costs=total_costs,
        tolerance=tolerance,
        tolerance_usd=tolerance * total_costs,
        max_abs_difference=max_difference,
        max_abs_residual=max_residual,
        residual_tolerance_usd=residual_tolerance_usd,
        within_tolerance=max_difference <= tolerance * total_costs,
        explained=bool((decisions["cause"] != "unexplained").all())
        and max_residual <= residual_tolerance_usd,
        screener=screener,
        adjusted=adjusted,
    )


def _as_timestamps(quotes: pd.DataFrame) -> pd.DataFrame:
    """Quotes with tz-aware ``ts_utc`` (the screener's input), from either timestamp form."""
    if pd.api.types.is_integer_dtype(quotes["ts_utc"]):
        return quotes.assign(ts_utc=pd.to_datetime(quotes["ts_utc"], unit="ns", utc=True))
    return quotes


def _daily(
    screener: BacktestResult, adjusted: BacktestResult, event: EventBacktestResult, capital: float
) -> pd.DataFrame:
    days = event.daily.index

    def equity(result: BacktestResult) -> pd.Series:
        # the screener's days start at its first fill; before it, equity is the capital
        return result.daily["equity"].reindex(days).ffill().fillna(capital)

    screen, adjust, events = equity(screener), equity(adjusted), event.daily["equity"]
    return pd.DataFrame(
        {
            "screener_equity": screen,
            "event_equity": events,
            "difference": events - screen,
            "execution_effect": adjust - screen,
            "residual": events - adjust,
        },
        index=days,
    )


def _decisions(
    positions: pd.Series, screener: BacktestResult, event: EventBacktestResult
) -> pd.DataFrame:
    """Every decision of either tier, what each did and the cause of any difference."""
    s_fills = screener.fills.set_index("decision_time")
    e_fills = event.fills.set_index("decision_time")
    fates = _event_fates(event.ledger)
    times = pd.DatetimeIndex(
        sorted(set(s_fills.index) | set(fates.index) | set(screener.missed) | set(screener.closed))
    )
    rows: list[dict[str, Any]] = []
    diverged = False
    for t in times:
        row: dict[str, Any] = {"decision_time": t, "target": positions.get(t, np.nan)}
        s_filled, e_filled = t in s_fills.index, t in e_fills.index
        if s_filled:
            fill = s_fills.loc[t]
            row.update(screener_fill=fill["fill_time"], screener_lots=fill["lots"])
        if e_filled:
            fill = e_fills.loc[t]
            row.update(event_fill=fill["fill_time"], event_lots=fill["lots"])
        fate = fates.get(t, "no intent")
        row["event"] = "filled" if e_filled else fate
        row["screener"] = (
            "filled"
            if s_filled
            else "missed"
            if t in screener.missed
            else "closed"
            if t in screener.closed
            else "no trade"
        )
        cause, detail = _cause(row, fate, diverged)
        if cause not in ("match", "unexplained"):
            diverged = True
        row.update(cause=cause, detail=detail)
        rows.append(row)
    columns = [
        "decision_time",
        "target",
        "screener",
        "screener_fill",
        "screener_lots",
        "event",
        "event_fill",
        "event_lots",
        "cause",
        "detail",
    ]
    return pd.DataFrame(rows, columns=columns)


def _cause(row: dict[str, Any], fate: str, diverged: bool) -> tuple[str, str]:
    """The cause of one decision's difference between the tiers ("match" when there is none)."""
    screener, event = row["screener"], row["event"]
    if fate.startswith("engine exit"):
        return "event rule", fate
    if screener == "filled" and event == "filled":
        if row["screener_fill"] != row["event_fill"]:
            return "unexplained", "the tiers filled on different quotes"
        if abs(row["screener_lots"] - row["event_lots"]) <= 1e-9:
            return "match", ""
        return "sizing", f"{row['screener_lots']:.6f} vs {row['event_lots']:.6f} lots"
    both_skipped = (
        (screener == "missed" and event == "expired")
        or (screener == "closed" and event.startswith("refused: market closed"))
        or (screener == "no trade" and event == "no order: target unchanged")
    )
    if both_skipped:
        return "match", ""
    if event == "no order: target unchanged":
        return "target unchanged after rounding", ""
    if event.startswith(("refused", "rejected", "cancelled")):
        return "event rule", event
    follows = (screener == "no trade" and event == "filled") or (
        screener == "filled" and event == "no intent"
    )
    if diverged and follows:
        return "follow-on", f"screener {screener}, event {event}"
    return "unexplained", f"screener {screener}, event {event}"


def _event_fates(ledger: pd.DataFrame) -> pd.Series:
    """What happened to each event intent, by decision time (engine exits are marked)."""
    intents = ledger.loc[ledger["kind"] == "intent"]
    by_intent: dict[str, str] = {}
    for kind, prefix in (
        ("refusal", "refused"),
        ("order_rejected", "rejected"),
        ("order_cancelled", "cancelled"),
        ("order_expired", "expired"),
    ):
        rows = ledger.loc[ledger["kind"] == kind]
        for intent_id, reason in zip(rows["intent_id"], rows["reason"], strict=True):
            if intent_id not in by_intent:
                by_intent[intent_id] = prefix if kind == "order_expired" else f"{prefix}: {reason}"
    decisions = ledger.loc[ledger["kind"] == "decision"]
    for intent_id, side, approved, reason in zip(
        decisions["intent_id"],
        decisions["side"],
        decisions["approved"],
        decisions["reason"],
        strict=True,
    ):
        if not approved:
            by_intent.setdefault(intent_id, f"rejected: {reason}")
        elif side is None or (isinstance(side, float) and np.isnan(side)):
            by_intent.setdefault(intent_id, "no order: target unchanged")
    fates = {}
    for t, intent_id, reason in zip(
        intents["ts"], intents["intent_id"], intents["reason"], strict=True
    ):
        fate = by_intent.get(intent_id, "ordered")
        if reason:
            fate = f"engine exit ({reason}): {fate}"
        fates[pd.Timestamp(t)] = fate
    return pd.Series(fates, dtype=object)
