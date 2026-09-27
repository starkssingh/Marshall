"""Limits and halts (RISK-003).

**Halts** refuse new exposure — an order that opens, increases or flips a position — and never
block an order that only reduces it. Each triggers exactly at its threshold (``>=``):

- **drawdown halt**: the worst drawdown since the start or the last manual reset is at least
  ``max_drawdown`` (sticky until `RiskStateTracker.reset_halt`);
- **daily loss halt**: the trading day's loss is at least ``max_daily_loss`` of its starting
  equity (lifted at the next trading day);
- **cooldown**: at least ``max_consecutive_losses`` losing round trips in a row, and fewer than
  ``cooldown_minutes`` since the last one closed;
- **trades per day**: ``max_trades_per_day`` entries have filled in the trading day.

**Caps** bound every target position, whatever the sizing asked for: at most ``max_lots``; a
notional (at the decision's reference price, plus any correlated exposure from other
instruments) of at most ``max_notional`` x equity; margin of at most ``max_margin_use`` x equity;
and, while a session named in ``session_max_exposure`` is in force, a notional of at most that
multiple of equity. A capped target is rounded down to the lot step, so it never exceeds a cap
(up to 5e-13 lots of floating-point representation, ADR 0052).

**Correlated exposure** is a hook for future instruments (`CorrelatedExposure`): the notional
the rest of the book adds to this instrument's. With one instrument there is none.
"""

from __future__ import annotations

from typing import Protocol

from xq.core.config import InstrumentSpec, RiskLimitsConfig
from xq.risk.state import MarketState, RiskState

_MINUTE_NS = 60_000_000_000


class CorrelatedExposure(Protocol):
    """Notional (USD) of correlated positions in other instruments, counted against the caps."""

    def extra_notional(self, state: RiskState) -> float:
        """The correlated notional to add to this instrument's."""
        ...


def entry_halts(state: RiskState, limits: RiskLimitsConfig, now: int) -> list[str]:
    """Every halt in force at `now` (module docstring); empty when new exposure is allowed."""
    reasons: list[str] = []
    if state.worst_drawdown >= limits.max_drawdown:
        reasons.append(
            f"drawdown halt: worst drawdown {state.worst_drawdown:.4%} reached "
            f"{limits.max_drawdown:.2%} (until a manual reset)"
        )
    if state.day_loss >= limits.max_daily_loss:
        reasons.append(
            f"daily loss halt: {state.day_loss:.4%} of the day's starting equity reached "
            f"{limits.max_daily_loss:.2%}"
        )
    if (
        state.consecutive_losses >= limits.max_consecutive_losses
        and state.last_loss_at is not None
        and now < state.last_loss_at + limits.cooldown_minutes * _MINUTE_NS
    ):
        reasons.append(
            f"cooldown: {state.consecutive_losses} losing round trips in a row, "
            f"{limits.cooldown_minutes} min from the last"
        )
    if state.trades_today >= limits.max_trades_per_day:
        reasons.append(f"trades per day: {state.trades_today} entries filled today")
    return reasons


def cap_target(
    target_lots: float,
    *,
    state: RiskState,
    market: MarketState,
    limits: RiskLimitsConfig,
    instrument: InstrumentSpec,
    margin_rate: float,
    price: float,
    correlated_notional: float = 0.0,
) -> tuple[float, list[str]]:
    """`target_lots` (signed) cut to every cap and rounded down; the caps that bound it."""
    contract = float(instrument.contract_size)
    per_lot = contract * price
    equity = max(state.equity, 0.0)
    bounds = {"max_lots": limits.max_lots}
    bounds["max_notional"] = max(0.0, limits.max_notional * equity - correlated_notional) / per_lot
    bounds["max_margin_use"] = limits.max_margin_use * equity / (per_lot * margin_rate)
    for session in market.sessions:
        cap = limits.session_max_exposure.get(session)
        if cap is not None:
            bounds[f"session {session}"] = cap * equity / per_lot
    size = abs(target_lots)
    reasons = []
    for name, bound in bounds.items():
        if size > bound:
            size = bound
            reasons.append(name)
    sign = 1.0 if target_lots >= 0 else -1.0
    rounded = float(instrument.round_lots(f"{size:.12f}")) if size > 0 else 0.0
    return sign * rounded if rounded else 0.0, [f"capped by {r}" for r in reasons]
