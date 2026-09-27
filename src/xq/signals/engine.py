"""Signal engine (SIGNAL-004): forecasts plus regime -> candidates -> filters -> trade intents.

A strategy is defined entirely by its YAML (`StrategySpec`, ``experiments/configs/strategies/``):
which model's forecasts of which barrier target it trades, on which sides, the barriers in
sigma-hat units, the EV thresholds, the requested exposure and its filters. At a decision time
`SignalEngine.decide` turns the forecasts made *at that time* into candidates and records every
one of them in a `SignalRecord`, including the rejected ones with their reasons:

1. every forecast of the strategy's model and target on one of its sides is a candidate; its
   entry reference is the side's quote (the ask for a long, the bid for a short), its stop and
   target ``sl_sigmas`` and ``tp_sigmas`` of the forecast's horizon sigma-hat away, its time stop
   the end of the horizon;
2. an **uncalibrated forecast is refused**, and so is one made at another time than the decision
   (a later one would be look-ahead, an earlier one stale) or without ``p_tp_first``;
3. the EV (SIGNAL-002) with the round-trip cost of the current quote — the spread, twice the cost
   model's slippage and commission — in sigma-hat units must clear ``theta`` and ``p_min``
   (with the conservative lower bound of p when the strategy asks for it);
4. every configured filter (SIGNAL-003) must let it through;
5. of the candidates left, the one with the highest EV_net becomes a `TradeIntent` (market entry,
   the strategy's requested exposure, the candidate's stop, target and time stop, and its
   calibrated probability for the risk engine's scaling); a candidate on the side already held is
   not selected. The others are recorded as not selected.

The engine never sizes: sizing, limits and the stop policy are the risk engine's (RISK-005), which
decides every intent. Thresholds come from the YAML and are chosen on validation folds only (plan
Phase 15).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from xq.backtest.costs import CostModel
from xq.core.config import SessionsConfig
from xq.core.time import from_ns, to_ns
from xq.data.calendar import regular_trading_day
from xq.signals.ev import cost_in_sigmas, expected_value
from xq.signals.filters import (
    BlackoutFilter,
    CalendarLookup,
    FilterContext,
    HourOfWeekSpreads,
    PassThroughRegimeFilter,
    RegimeFilter,
    SessionFilter,
    SignalFilter,
    SpreadFilter,
    VolatilityBandFilter,
)
from xq.signals.schema import Forecast, RegimeState, SignalCandidate, SignalRecord, TradeIntent

Side = Literal["long", "short"]
_BPS = 1e-4
UNCALIBRATED = "uncalibrated forecast: refused (SIGNAL-004)"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ForecastSpec(_Frozen):
    """Which forecasts the strategy trades."""

    model_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)


class BarrierSpec(_Frozen):
    """Take-profit and stop-loss distances in units of the forecast's horizon sigma-hat."""

    tp_sigmas: float = Field(gt=0)
    sl_sigmas: float = Field(gt=0)


class ConservativeSpec(_Frozen):
    """The conservative EV variant: p is replaced by ``p - z x p_se``."""

    z: float = Field(gt=0)


class EVSpec(_Frozen):
    """EV thresholds (strict), in sigma-hat units and probability."""

    theta: float
    p_min: float = Field(ge=0, lt=1)
    conservative: ConservativeSpec | None = None


class BandSpec(_Frozen):
    """A daily sigma-hat band (fractions of price)."""

    low: float = Field(ge=0)
    high: float = Field(gt=0)


class SpreadSpec(_Frozen):
    """The spread filter: below `k` x its hour-of-week median of at least `min_obs` spreads."""

    k: float = Field(gt=0)
    min_obs: int = Field(ge=1)


class FiltersSpec(_Frozen):
    """The strategy's filters (SIGNAL-003); empty lists and nulls switch a filter off."""

    regimes: list[str] = []
    sessions: list[str] = []
    blackouts: list[str] = []
    volatility_band: BandSpec | None = None
    spread: SpreadSpec | None = None


class StrategySpec(_Frozen):
    """A signal strategy, defined entirely by its YAML (module docstring)."""

    strategy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    instrument: str = Field(min_length=1)
    forecast: ForecastSpec
    sides: list[Side] = Field(min_length=1)
    horizon: timedelta
    barriers: BarrierSpec
    ev: EVSpec
    exposure: float = Field(gt=0)
    filters: FiltersSpec = FiltersSpec()

    @field_validator("horizon", mode="before")
    @classmethod
    def _duration(cls, value: Any) -> Any:
        if isinstance(value, str):
            return pd.Timedelta(value).to_pytimedelta()
        return value


def load_strategy_spec(path: Path) -> StrategySpec:
    """A strategy YAML (``experiments/configs/strategies/*.yaml``), validated."""
    return StrategySpec.model_validate(yaml.safe_load(path.read_text()))


@dataclass(frozen=True)
class _Built:
    """A candidate with its record id and why it cannot become an intent (empty: it can)."""

    candidate: SignalCandidate
    record_id: str
    reasons: list[str]
    ev_reasons: tuple[str, ...]

    @property
    def sign(self) -> int:
        return 1 if self.candidate.direction == "long" else -1


@dataclass(frozen=True)
class SignalDecision:
    """What one decision produced: at most one intent, and a record for every candidate."""

    intents: list[TradeIntent]
    records: list[SignalRecord]


class SignalEngine:
    """Turns forecasts into trade intents for one strategy (module docstring)."""

    def __init__(
        self,
        spec: StrategySpec,
        costs: CostModel,
        sessions: SessionsConfig,
        *,
        regime_filter: RegimeFilter | None = None,
    ) -> None:
        self.spec = spec
        self.costs = costs
        self.spreads = HourOfWeekSpreads(spec.filters.spread.min_obs if spec.filters.spread else 1)
        self._minutes_per_day = regular_trading_day(sessions) / pd.Timedelta(minutes=1)
        filters = spec.filters
        self.filters: list[SignalFilter] = [
            regime_filter if regime_filter is not None else PassThroughRegimeFilter(filters.regimes)
        ]
        if filters.sessions or filters.blackouts:
            calendar = CalendarLookup(sessions)
            if filters.sessions:
                self.filters.append(SessionFilter(calendar, filters.sessions))
            if filters.blackouts:
                self.filters.append(BlackoutFilter(calendar, filters.blackouts))
        if filters.volatility_band is not None:
            band = filters.volatility_band
            self.filters.append(VolatilityBandFilter(band.low, band.high))
        if filters.spread is not None:
            self.filters.append(SpreadFilter(filters.spread.k, self.spreads))
        self._ids = itertools.count(1)

    def observe_spread(self, ts: int, spread: float) -> None:
        """A spread known from `ts` on, for the hour-of-week reference (after deciding at `ts`)."""
        self.spreads.observe(ts, spread)

    def round_trip_cost_bps(self, ts: int, bid: float, ask: float, sigma_daily: float) -> float:
        """Spread plus twice the slippage and commission of the cost model, in bps of the mid.

        The slippage's sigma term takes the daily sigma-hat scaled to one minute of market time.
        """
        mid = (bid + ask) / 2
        sigma_1m_bps = sigma_daily / math.sqrt(self._minutes_per_day) / _BPS
        slippage = self.costs.slippage_bps_at(ts, sigma_1m_bps)
        contract = float(self.costs.instrument.contract_size)
        commission = float(self.costs.commission_usd([1.0], [mid])[0]) / (contract * mid) / _BPS
        return (ask - bid) / mid / _BPS + 2 * slippage + 2 * commission

    def decide(
        self,
        ts: int,
        forecasts: Sequence[Forecast],
        *,
        bid: float,
        ask: float,
        sigma_daily: float | None,
        regime: RegimeState | None = None,
        position_lots: float = 0.0,
    ) -> SignalDecision:
        """Candidates, records and at most one intent at decision time `ts` (module docstring).

        Raises:
            ValueError: if one of the strategy's forecasts carries no ``p_tp_first``.
        """
        spec = self.spec
        mine = [
            f
            for f in forecasts
            if f.model_id == spec.forecast.model_id
            and f.target_id == spec.forecast.target_id
            and f.instrument == spec.instrument
            and f.side in spec.sides
        ]
        context = FilterContext(ts, ask - bid, sigma_daily, regime)
        outcomes = {f.name: f.check(context) for f in self.filters}
        filter_record = {
            f.name: f.pass_label if outcomes[f.name] is None else str(outcomes[f.name])
            for f in self.filters
        }
        blocking = [f"filter {name}: {why}" for name, why in outcomes.items() if why is not None]
        held = 0 if position_lots == 0 else (1 if position_lots > 0 else -1)
        built = [self._candidate(ts, f, bid, ask, sigma_daily, regime, blocking) for f in mine]
        eligible = [b for b in built if not b.reasons and b.sign != held]
        chosen = max(eligible, key=lambda b: b.candidate.ev_net, default=None)
        records: list[SignalRecord] = []
        intents: list[TradeIntent] = []
        for b, forecast in zip(built, mine, strict=True):
            fields: dict[str, Any] = {
                "record_id": b.record_id,
                "candidate": b.candidate,
                "forecasts": (forecast,),
                "model_versions": {forecast.model_id: forecast.model_version},
                "feature_set_versions": (forecast.feature_set_version,),
                "sigma_daily": sigma_daily,
                "spread": ask - bid,
                "filters": filter_record,
                "ev_reasons": b.ev_reasons,
            }
            if b.reasons:
                records.append(SignalRecord(**fields, outcome="rejected", reasons=tuple(b.reasons)))
            elif b is chosen:
                intent = self._intent(b.candidate, b.record_id)
                intents.append(intent)
                records.append(SignalRecord(**fields, outcome="intent", intent=intent))
            elif b.sign == held:
                why = "a position on this side is already held"
                records.append(SignalRecord(**fields, outcome="not selected", reasons=(why,)))
            else:
                assert chosen is not None  # b itself was eligible
                why = f"{chosen.record_id} has a higher EV_net ({chosen.candidate.ev_net:.4f})"
                records.append(SignalRecord(**fields, outcome="not selected", reasons=(why,)))
        return SignalDecision(intents, records)

    def _candidate(
        self,
        ts: int,
        forecast: Forecast,
        bid: float,
        ask: float,
        sigma_daily: float | None,
        regime: RegimeState | None,
        blocking: list[str],
    ) -> _Built:
        spec = self.spec
        if forecast.p_tp_first is None:
            raise ValueError(f"forecast {forecast.forecast_id} carries no p_tp_first")
        record_id = f"S-{spec.strategy_id}-{next(self._ids):06d}"
        reasons: list[str] = []
        if not forecast.calibrated:
            reasons.append(UNCALIBRATED)
        if to_ns(forecast.ts) != ts:
            reasons.append(f"forecast made at {forecast.ts}, not at the decision time")
        long = forecast.side == "long"
        sign = 1.0 if long else -1.0
        entry = ask if long else bid
        sigma = forecast.sigma_hat
        barriers = spec.barriers
        stop = entry - sign * barriers.sl_sigmas * sigma * entry
        target = entry + sign * barriers.tp_sigmas * sigma * entry
        # without a daily sigma-hat the slippage's sigma term uses the forecast's
        cost_bps = self.round_trip_cost_bps(ts, bid, ask, sigma_daily or sigma)
        cost = cost_in_sigmas(cost_bps, sigma)
        conservative = spec.ev.conservative
        p_se = forecast.p_se if conservative is not None else None
        if conservative is not None and p_se is None:
            reasons.append("the conservative EV variant needs the forecast's p_se")
        ev = expected_value(
            forecast.p_tp_first,
            tp=barriers.tp_sigmas,
            sl=barriers.sl_sigmas,
            cost=cost,
            theta=spec.ev.theta,
            p_min=spec.ev.p_min,
            p_se=p_se,
            z=conservative.z if conservative is not None and p_se is not None else None,
        )
        reasons.extend(ev.reasons)
        reasons.extend(blocking)
        candidate = SignalCandidate(
            candidate_id=record_id,
            ts=from_ns(ts),
            instrument=spec.instrument,
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            direction=forecast.side,
            entry_ref=entry,
            stop=stop,
            target=target,
            horizon=spec.horizon,
            p_forecast=forecast.p_tp_first,
            p_win=ev.p_used,
            payoff_ratio=ev.payoff_ratio,
            ev_gross=ev.gross,
            ev_costs=cost,
            ev_net=ev.net,
            sigma_hat=sigma,
            regime=regime,
            forecast_ids=(forecast.forecast_id,),
        )
        return _Built(candidate, record_id, reasons, ev.reasons)

    def _intent(self, candidate: SignalCandidate, record_id: str) -> TradeIntent:
        return TradeIntent(
            direction=candidate.direction,
            exposure=self.spec.exposure,
            stop=candidate.stop,
            target=candidate.target,
            time_stop=candidate.ts + candidate.horizon,
            p_win=candidate.p_win,
            calibrated=True,
            strategy_id=self.spec.strategy_id,
            signal_id=record_id,
        )
