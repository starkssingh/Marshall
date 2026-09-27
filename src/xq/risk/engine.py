"""The risk engine (RISK-005): every intent becomes a `RiskDecision` here, and only here.

`RiskEngine.evaluate(intent, state, market)` is a pure, deterministic function of the intent,
the risk state (`RiskState`, RISK-001) and the market state (`MarketState`): the same inputs
give the same decision, and it reads no clock, file or environment. Every decision it returns
is *issued* (`issue_decision`), the only kind an `OrderIntent` can be built from
(`xq.signals.schema`); the event engine records every decision in the decision ledger.

**Exits are always approved.** A ``flat`` intent closes the position with a market order, whatever
halts, breakers or missing data are in force.

**New exposure** — an intent whose target opens, increases or flips the position — passes, in
order:

1. the data it needs and the switches: a quote at the decision, a win probability that is
   calibrated if the intent carries one (an uncalibrated probability never sizes a position), the
   kill switch off and no data-health breaker tripped (RISK-006, `xq.risk.kill_switch`);
2. the stop policy (RISK-004, `check_stops`): the stop the decision accepts, possibly widened;
3. sizing (RISK-002, `size_entry`): the target size from the risk budget to that stop (or the
   volatility target), capped by the requested exposure, scaled by the edge per unit of risk of a
   calibrated probability (its lower confidence bound against the target, the stop and the
   round-trip cost of the engine's own cost model, ADR 0053) and by the drawdown throttle, rounded
   down to the lot step — at the decision's mid, with the equity of the risk state;
4. the halts (RISK-003, `entry_halts`) when the sized target adds exposure;
5. the caps (RISK-003, `cap_target`) on the target, rounded down to the lot step.

An intent refused at steps 1, 2 or 4 is **rejected**, except that a position on the other side
is still closed: the strategy no longer wants it, and closing it only reduces risk. That exit is
approved with a first reason starting ``risk rule:``. An intent whose sized target only reduces
the position on the same side is not subject to the halts. The order is the difference between
the approved target and the position the state saw; a target equal to the position needs no
order.

``limits_snapshot`` records everything the decision used — the risk state, the limits, the market,
the stop and every sizing step — and ``config_version`` is the profile's ``version`` with the first
12 hex digits of the SHA-256 of the whole profile, so a decision names the exact rules it applied.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from xq.backtest.costs import CostModel
from xq.backtest.events import clean_lots
from xq.core.config import AppConfig, InstrumentSpec, RiskConfig
from xq.core.time import to_ns
from xq.core.types import Side
from xq.risk.kill_switch import breaker_reasons
from xq.risk.limits import CorrelatedExposure, cap_target, entry_halts
from xq.risk.sizing import Edge, size_entry
from xq.risk.state import MarketState, RiskState
from xq.risk.stops import check_stops
from xq.signals.schema import EntryType, RiskDecision, TradeIntent

#: First words of the reason of an approved decision that a risk rule, not sizing, shaped.
RISK_RULE = "risk rule: "


def issue_decision(**fields: Any) -> RiskDecision:
    """A validated `RiskDecision` marked as issued by the risk engine.

    Only `RiskEngine` calls this: the architectural test fails on any other call under ``src/``.
    """
    decision = RiskDecision(**fields)
    decision._issued = True
    return decision


def profile_hash(config: RiskConfig) -> str:
    """First 12 hex digits of the SHA-256 of the profile's canonical JSON."""
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


class RiskEngine:
    """Turns intents into issued risk decisions (module docstring)."""

    def __init__(
        self,
        config: RiskConfig,
        instrument: InstrumentSpec,
        *,
        margin_rate: float,
        costs: CostModel,
        periods_per_year: int = 252,
        correlated: CorrelatedExposure | None = None,
    ) -> None:
        if not 0 < margin_rate <= 1:
            raise ValueError("margin_rate must be in (0, 1]")
        self.config = config
        self.instrument = instrument
        self.costs = costs
        self.margin_rate = float(margin_rate)
        self.periods_per_year = periods_per_year
        self.correlated = correlated
        self.config_version = f"{config.version}@{profile_hash(config)}"
        self.tick = float(instrument.tick_size)

    @classmethod
    def from_config(
        cls, cfg: AppConfig, instrument: str = "xauusd", *, profile: str | None = None
    ) -> RiskEngine:
        """The engine of risk profile `profile` (default ``backtest.risk_profile``), pricing costs
        with the configured cost model."""
        event = cfg.backtest_config().event_config()
        return cls(
            cfg.risk_config(profile),
            cfg.instrument(instrument),
            margin_rate=event.margin_rate,
            costs=CostModel.from_config(cfg, instrument),
        )

    @property
    def label(self) -> str:
        """How results produced with this engine are labelled."""
        marker = " (PROVISIONAL risk profile)" if self.config.provisional else ""
        return f"RiskEngine {self.config_version}{marker}"

    @property
    def watched_sessions(self) -> tuple[str, ...]:
        """Sessions whose membership the market state must report (the session caps)."""
        return tuple(self.config.limits.session_max_exposure)

    def evaluate(
        self, intent: TradeIntent, state: RiskState, market: MarketState | None
    ) -> RiskDecision:
        """The issued decision on `intent` (module docstring).

        Raises:
            ValueError: if the intent has not been stamped with its id and decision time.
        """
        if intent.intent_id is None or intent.created_at is None:
            raise ValueError("the engine stamps intent_id and created_at before the risk decision")
        base: dict[str, Any] = {
            "decision_id": f"D-{intent.intent_id}",
            "intent_id": intent.intent_id,
            "decided_at": intent.created_at,
            "config_version": self.config_version,
        }
        position = state.position_lots
        snapshot = self._snapshot(state, market)
        if intent.direction == "flat":
            reason = "exit: always allowed" if position else "exit: no position to close"
            return self._decide(base, 0.0, position, [reason], snapshot)
        blocks = self._blocks(intent, market)
        if blocks or market is None:
            return self._refuse(base, intent, position, blocks, snapshot)
        long = intent.direction == "long"
        side_quote = market.ask if long else market.bid
        reference = side_quote if intent.limit_price is None else intent.limit_price
        stops = check_stops(
            intent.direction,
            stop=intent.stop,
            target=intent.target,
            time_stop=None if intent.time_stop is None else to_ns(intent.time_stop),
            now=to_ns(intent.created_at),
            reference=reference,
            spread=market.spread,
            sigma_daily=market.sigma_daily,
            policy=self.config.stops,
            tick=self.tick,
        )
        snapshot.update(entry_reference=reference, stop_distance=stops.distance)
        if not stops.accepted:
            return self._refuse(base, intent, position, list(stops.reasons), snapshot)
        edge = None
        if intent.p_win is not None and intent.p_se is not None and intent.target is not None:
            cost_bps = self.costs.round_trip_cost_bps(
                to_ns(intent.created_at), market.bid, market.ask, market.sigma_daily or 0.0
            )
            edge = Edge(
                intent.p_win,
                intent.p_se,
                abs(intent.target - reference),
                cost_bps * 1e-4 * market.mid,
            )
            snapshot.update(round_trip_cost=edge.cost, target_distance=edge.target_distance)
        sizing = size_entry(
            self.config.sizing,
            self.instrument,
            equity=state.equity,
            price=market.mid,
            stop_distance=stops.distance,
            sigma_daily=market.sigma_daily,
            periods_per_year=self.periods_per_year,
            requested_exposure=intent.exposure,
            edge=edge,
            drawdown=state.drawdown,
        )
        snapshot.update(sizing.as_snapshot())
        desired = intent.sign * sizing.lots
        if _adds_exposure(position, desired):
            halts = entry_halts(state, self.config.limits, to_ns(intent.created_at))
            if halts:
                return self._refuse(base, intent, position, halts, snapshot)
        correlated = 0.0 if self.correlated is None else self.correlated.extra_notional(state)
        target, capped = cap_target(
            desired,
            state=state,
            market=market,
            limits=self.config.limits,
            instrument=self.instrument,
            margin_rate=self.margin_rate,
            price=market.mid,
            correlated_notional=correlated,
        )
        snapshot.update(approved_target_lots=target, correlated_notional=correlated)
        reasons = ["entry: sized by the risk engine", *stops.notes, *capped]
        if sizing.lots == 0:
            reasons.append("sized to zero lots")
        return self._decide(
            base,
            target,
            position,
            reasons,
            snapshot,
            order_type=intent.entry_type,
            price=intent.limit_price,
            stop=stops.stop,
            take_profit=intent.target,
        )

    # --- helpers --------------------------------------------------------------------------------

    def _blocks(self, intent: TradeIntent, market: MarketState | None) -> list[str]:
        """What stops any new exposure before the stop policy: data, kill switch, breakers."""
        blocks: list[str] = []
        if market is None:
            blocks.append("no quote known at the decision time")
        else:
            if market.kill_reason is not None:
                blocks.append(market.kill_reason)
            blocks.extend(breaker_reasons(market, self.config.breakers))
        if intent.p_win is not None:
            if not intent.calibrated:
                blocks.append("an uncalibrated win probability cannot size a position")
            if intent.p_se is None:
                blocks.append("a win probability needs its standard error (p_se) to size on")
            if intent.target is None:
                blocks.append("a win probability needs a target to price the payoff")
        return blocks

    def _refuse(
        self,
        base: dict[str, Any],
        intent: TradeIntent,
        position: float,
        reasons: list[str],
        snapshot: dict[str, float | str],
    ) -> RiskDecision:
        """Reject new exposure; a position on the other side is still closed."""
        if position != 0 and np.sign(position) != intent.sign:
            why = [f"{RISK_RULE}{'; '.join(reasons)}", "exit only: the opposite position is closed"]
            return self._decide(base, 0.0, position, why, snapshot)
        return issue_decision(
            **base, approved=False, reasons=tuple(reasons), limits_snapshot=snapshot
        )

    def _decide(
        self,
        base: dict[str, Any],
        target: float,
        position: float,
        reasons: list[str],
        snapshot: dict[str, float | str],
        *,
        order_type: EntryType = "market",
        price: float | None = None,
        stop: float | None = None,
        take_profit: float | None = None,
    ) -> RiskDecision:
        """An approved decision moving the position to `target`."""
        target = clean_lots(target)
        size = clean_lots(target - position)
        if size == 0:
            reasons = [*reasons, "target unchanged: no order"]
        if target == 0:  # a closing order is a plain market exit
            order_type, price, stop, take_profit = "market", None, None, None
        return issue_decision(
            **base,
            approved=True,
            side=None if size == 0 else (Side.BUY if size > 0 else Side.SELL),
            size_lots=abs(size),
            target_lots=target,
            order_type=order_type,
            price=price,
            adjusted_stop=stop,
            target=take_profit,
            reasons=tuple(reasons),
            limits_snapshot=snapshot,
        )

    def _snapshot(self, state: RiskState, market: MarketState | None) -> dict[str, float | str]:
        limits = self.config.limits
        snapshot: dict[str, float | str] = {
            **state.snapshot(),
            "max_lots": limits.max_lots,
            "max_notional": limits.max_notional,
            "max_margin_use": limits.max_margin_use,
            "max_daily_loss": limits.max_daily_loss,
            "max_drawdown": limits.max_drawdown,
            "max_consecutive_losses": float(limits.max_consecutive_losses),
            "max_trades_per_day": float(limits.max_trades_per_day),
            "margin_rate": self.margin_rate,
        }
        if market is not None:
            snapshot.update(
                bid=market.bid,
                ask=market.ask,
                quote_age_s=market.quote_age_s,
                sessions=",".join(market.sessions),
            )
            if market.sigma_daily is not None:
                snapshot["sigma_daily"] = market.sigma_daily
            if market.spread_reference is not None:
                snapshot["spread_reference"] = market.spread_reference
            if market.kill_reason is not None:
                snapshot["kill_switch"] = market.kill_reason
        return snapshot


def _adds_exposure(before: float, after: float) -> bool:
    """True when moving from `before` to `after` opens, increases or flips the position."""
    if after == 0:
        return False
    return before == 0 or np.sign(after) != np.sign(before) or abs(after) > abs(before)
