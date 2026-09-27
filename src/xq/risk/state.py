"""Risk state and market state: what `RiskEngine.evaluate` decides on (RISK-001).

**Risk state** (`RiskState`) is derived from the account, never from a model:

- equity, peak equity, the current drawdown ``(peak - equity) / peak`` and the worst drawdown
  since the start or the last manual reset (the drawdown halt is sticky: RISK-003);
- the trading day (17:00 New York roll), its starting equity (the equity at the previous day's
  end, or the first observation) and its P&L;
- the position, its notional at the mark and the margin it uses;
- consecutive losing round trips (flat to flat, or flat to a flip; realized price P&L net of
  commissions, financing excluded) and when the last loss closed;
- entries filled in the trading day (fills that open, increase or flip exposure).

Equity is *observed* at every risk decision and at every trading day's end — the instants the
engine records as ``account`` rows in the decision ledger — and fills arrive as the ledger's
``fill`` rows. `RiskStateTracker` updates the state from those two streams, and
`rebuild_risk_state` replays them from a ledger, so the state after a restart is the state the
live system had (tested at every decision of engine runs).

**Market state** (`MarketState`) is what the risk engine knows about the market at the decision:
the latest quote and its age, the daily sigma-hat, a reference spread for the abnormal-spread
breaker, the sessions in force and whether the kill switch is on. The engine assembles it, so
`RiskEngine.evaluate` stays a pure function of its inputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from xq.backtest.events import AccountState, Fill, clean_lots
from xq.core.time import trading_day

_NS_PER_S = 1_000_000_000


@dataclass(frozen=True)
class RiskState:
    """The account as the risk engine sees it (module docstring)."""

    ts: int
    trading_day: date
    capital: float
    equity: float
    peak_equity: float
    drawdown: float
    worst_drawdown: float
    day_start_equity: float
    day_pnl: float
    position_lots: float
    mark: float
    open_notional: float
    margin_used: float
    consecutive_losses: int
    last_loss_at: int | None
    trades_today: int

    @property
    def day_loss(self) -> float:
        """The trading day's loss as a share of its starting equity (0 when it is up)."""
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, -self.day_pnl) / self.day_start_equity

    def snapshot(self) -> dict[str, float | str]:
        """The state's values for audit records (a decision's ``limits_snapshot``)."""
        return {
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "drawdown": self.drawdown,
            "worst_drawdown": self.worst_drawdown,
            "day_start_equity": self.day_start_equity,
            "day_pnl": self.day_pnl,
            "position_lots": self.position_lots,
            "open_notional": self.open_notional,
            "margin_used": self.margin_used,
            "consecutive_losses": float(self.consecutive_losses),
            "trades_today": float(self.trades_today),
            "trading_day": self.trading_day.isoformat(),
        }


@dataclass(frozen=True)
class MarketState:
    """The market at a decision (module docstring). ``sigma_daily`` is a fraction of price."""

    ts: int
    bid: float
    ask: float
    quote_ts: int
    sigma_daily: float | None = None
    spread_reference: float | None = None
    sessions: tuple[str, ...] = ()
    kill_reason: str | None = None

    @property
    def mid(self) -> float:
        """Mid of the latest quote."""
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        """Spread of the latest quote."""
        return self.ask - self.bid

    @property
    def quote_age_s(self) -> float:
        """Seconds since the latest quote."""
        return (self.ts - self.quote_ts) / _NS_PER_S


class RiskStateTracker:
    """Updates the risk state from equity observations and fills (module docstring)."""

    def __init__(self, capital: float, contract_size: float) -> None:
        if capital <= 0:
            raise ValueError("capital must be positive")
        self.capital = float(capital)
        self.contract = float(contract_size)
        self._account: AccountState | None = None
        self._day: date | None = None
        self._day_start = self.capital
        self._close_equity: float | None = None
        self._peak = self.capital
        self._worst = 0.0
        self._position = 0.0
        self._trades_today = 0
        self._losses = 0
        self._last_loss_at: int | None = None
        self._episode = 0.0

    def observe(self, account: AccountState) -> None:
        """Equity observed at a risk decision."""
        self._roll(trading_day(pd.Timestamp(account.ts, tz="UTC")))
        self._see(account)

    def close_day(self, account: AccountState) -> None:
        """Equity at a trading day's end: the next day's starting equity."""
        self._see(account)
        self._close_equity = account.equity

    def on_fill(self, fill: Fill) -> None:
        """A booked fill: position, entries today and round-trip outcomes."""
        self._roll(trading_day(pd.Timestamp(fill.ts, tz="UTC")))
        if fill.role == "entry":
            self._trades_today += 1
        before = self._position
        after = clean_lots(before + fill.lots)
        flow = -fill.lots * fill.price * self.contract - fill.commission
        if before != 0 and (after == 0 or np.sign(after) != np.sign(before)):
            share = abs(before / fill.lots)  # the part of the fill that closes the round trip
            self._close_round_trip(self._episode + share * flow, fill.ts)
            self._episode = (1 - share) * flow if after != 0 else 0.0
        else:
            self._episode += flow
        self._position = after

    def reset_halt(self, account: AccountState) -> None:
        """Manual reset of the drawdown halt: the peak restarts at the current equity."""
        self._see(account)
        self._peak = account.equity
        self._worst = 0.0

    def state(self) -> RiskState:
        """The state at the latest observation.

        Raises:
            ValueError: before any observation.
        """
        account = self._account
        if account is None or self._day is None:
            raise ValueError("the risk state needs an equity observation first")
        mark = account.mark if np.isfinite(account.mark) else 0.0
        return RiskState(
            ts=account.ts,
            trading_day=self._day,
            capital=self.capital,
            equity=account.equity,
            peak_equity=self._peak,
            drawdown=_drawdown(self._peak, account.equity),
            worst_drawdown=self._worst,
            day_start_equity=self._day_start,
            day_pnl=account.equity - self._day_start,
            position_lots=self._position,
            mark=mark,
            open_notional=abs(self._position) * self.contract * mark,
            margin_used=account.margin_used,
            consecutive_losses=self._losses,
            last_loss_at=self._last_loss_at,
            trades_today=self._trades_today,
        )

    def _see(self, account: AccountState) -> None:
        self._account = account
        self._peak = max(self._peak, account.equity)
        self._worst = max(self._worst, _drawdown(self._peak, account.equity))

    def _roll(self, day: date) -> None:
        if self._day == day:
            return
        if self._day is not None:
            if self._close_equity is not None:
                self._day_start = self._close_equity
            elif self._account is not None:
                self._day_start = self._account.equity
        self._day = day
        self._close_equity = None
        self._trades_today = 0

    def _close_round_trip(self, pnl: float, ts: int) -> None:
        if pnl < 0:
            self._losses += 1
            self._last_loss_at = ts
        else:
            self._losses = 0


def _drawdown(peak: float, equity: float) -> float:
    return max(0.0, (peak - equity) / peak) if peak > 0 else 0.0


def rebuild_risk_state(
    ledger: pd.DataFrame, *, capital: float, contract_size: float, upto_seq: int | None = None
) -> RiskState:
    """Replay a decision ledger's ``account`` and ``fill`` rows into the risk state.

    Args:
        ledger: The ledger frame (`xq.backtest.ledger.Ledger.frame`).
        capital: The account's starting capital.
        contract_size: Units per lot.
        upto_seq: Replay rows with ``seq`` up to and including this one (default: all).
    """
    tracker = RiskStateTracker(capital, contract_size)
    rows = ledger if upto_seq is None else ledger.loc[ledger["seq"] <= upto_seq]
    for row in rows.to_dict("records"):
        kind = row["kind"]
        if kind not in ("account", "fill"):
            continue
        detail = json.loads(str(row["detail"]))
        ts = int(pd.Timestamp(row["ts"]).value)
        if kind == "account":
            mark = detail["mark"]
            account = AccountState(
                ts=ts,
                capital=float(detail["capital"]),
                cash=float(detail["cash"]),
                unrealized=float(detail["unrealized"]),
                equity=float(detail["equity"]),
                position_lots=float(detail["position_lots"]),
                margin_used=float(detail["margin_used"]),
                mark=float(mark) if mark is not None else float("nan"),
            )
            if detail["event"] == "day_end":
                tracker.close_day(account)
            else:
                tracker.observe(account)
            continue
        sign = 1.0 if row["side"] == "buy" else -1.0
        tracker.on_fill(
            Fill(
                fill_id=str(row["fill_id"]),
                order_id=str(row["order_id"]),
                decision_id=str(row["decision_id"]),
                intent_id=str(row["intent_id"]),
                ts=ts,
                decided_at=ts,
                role=str(row["role"]),
                order_type=str(row["order_type"]),
                lots=sign * float(row["lots"]),
                price=float(row["price"]),
                bid=float(detail["bid"]),
                ask=float(detail["ask"]),
                mid=float(detail["mid"]),
                slippage_bps=float(detail["slippage_bps"]),
                spread_cost=float(detail["spread_cost"]),
                slippage_cost=float(detail["slippage_cost"]),
                commission=float(detail["commission"]),
                position_after=float(detail["position_after"]),
            )
        )
    return tracker.state()
