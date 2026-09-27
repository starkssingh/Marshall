"""Strategies shared by both backtest tiers (BT-009, BT-010), and the signal engine's adapter.

The vectorized screener takes target exposures per decision time; these strategies express the
same decisions to the event engine as market-order intents, so a strategy can run through both
tiers and be reconciled:

- `ExposureStrategy` replays a series of target exposures decided in advance.
- `RuleStrategy` runs a BASE-002 rule baseline itself, bar by bar, on the signal bars it has seen
  so far (so it can only be causal), and targets the rule's latest exposure.

Both behave like the screener: an intent is sent when the target differs from the exposure of the
last target that was actually executed, so a missed, refused or expired decision is retried at
the next decision. The exposure is a request to the risk engine, which sizes it; they never size.

`SignalStrategy` (SIGNAL-005) runs the signal engine (`xq.signals.engine.SignalEngine`) inside
the event backtester: at each signal bar it asks a forecast source for the forecasts made at that
decision time, lets the engine turn them into at most one intent, and keeps every forecast and
every `SignalRecord`. An intent's ``signal_id`` is its record's id, so the decision ledger links
each fill, through its order, risk decision and intent, back to the signal record and the
forecasts behind it (`SignalStrategy.audit`). It is event-tier only: the screener has no stops.

Every long or short intent of the exposure strategies carries a protective stop ``stop_sigmas``
daily sigma-hats (the value in the strategy's context, the one the risk engine uses) from the mid,
on the losing side of the entry quote — the stop policy (RISK-004) requires one. Without a
sigma-hat or a quote the intent goes without a stop, and the risk engine refuses it. The screener
has no stops: a reconciliation of the two tiers is only meaningful while no stop fills.
"""

from __future__ import annotations

import json
from typing import Protocol

import pandas as pd

from xq.backtest.engine import Strategy, StrategyContext
from xq.backtest.events import Bar, Fill
from xq.core.time import from_ns
from xq.models.baselines import RuleStrategyConfig, VolTargetConfig, rule_exposure
from xq.signals.engine import SignalEngine
from xq.signals.schema import Direction, Forecast, RegimeState, SignalRecord, TradeIntent


class _TargetStrategy(Strategy):
    """Sends a market intent when the target differs from the last executed target."""

    def __init__(self, stop_sigmas: float) -> None:
        if stop_sigmas <= 0:
            raise ValueError("stop_sigmas must be positive")
        self.stop_sigmas = stop_sigmas
        self.held = 0.0
        self._pending: float | None = None

    def _intents(self, target: float, ctx: StrategyContext) -> list[TradeIntent]:
        self._pending = None
        if target == self.held:
            return []
        self._pending = target
        if target == 0:
            return [TradeIntent(direction="flat", strategy_id=self.strategy_id)]
        direction: Direction = "long" if target > 0 else "short"
        stop = None
        if ctx.quote is not None and ctx.sigma_daily is not None:
            distance = self.stop_sigmas * ctx.sigma_daily * ctx.quote.mid
            stop = ctx.quote.ask - distance if target > 0 else ctx.quote.bid + distance
        return [
            TradeIntent(
                direction=direction,
                exposure=abs(target),
                stop=stop if stop is None or stop > 0 else None,
                strategy_id=self.strategy_id,
            )
        ]

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        """A fill of the pending target makes it the held target; an exit it did not ask for
        (a weekend exit, say) leaves nothing held."""
        if self._pending is not None:
            self.held, self._pending = self._pending, None
        elif fill.position_after == 0:
            self.held = 0.0


class ExposureStrategy(_TargetStrategy):
    """Replays target exposures decided in advance (the screener's positions)."""

    def __init__(
        self, positions: pd.Series, *, strategy_id: str = "exposure", stop_sigmas: float = 3.0
    ) -> None:
        super().__init__(stop_sigmas)
        index = pd.DatetimeIndex(positions.index)
        if index.tz is None:
            raise ValueError("positions must be indexed by tz-aware decision times")
        self.targets = {
            int(t.value): float(v)
            for t, v in zip(index.tz_convert("UTC").as_unit("ns"), positions, strict=True)
        }
        self.strategy_id = strategy_id

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[TradeIntent]:
        """The scheduled target at this decision time, if one is scheduled."""
        target = self.targets.get(ctx.now)
        return [] if target is None else self._intents(target, ctx)


class RuleStrategy(_TargetStrategy):
    """A BASE-002 rule baseline computed bar by bar on the signal bars seen so far."""

    def __init__(
        self,
        rule: RuleStrategyConfig,
        *,
        vol_target: VolTargetConfig | None = None,
        periods_per_year: int = 252,
        strategy_id: str | None = None,
        stop_sigmas: float = 3.0,
    ) -> None:
        super().__init__(stop_sigmas)
        self.rule = rule
        self.vol_target = vol_target
        self.periods_per_year = periods_per_year
        self.strategy_id = strategy_id or rule.rule
        self._bars: list[Bar] = []

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[TradeIntent]:
        """The rule's exposure on the bars available now."""
        self._bars.append(bar)
        exposure = rule_exposure(
            signal_frame(self._bars),
            self.rule,
            vol_target=self.vol_target,
            periods_per_year=self.periods_per_year,
        )
        return self._intents(float(exposure.iloc[-1]), ctx)


def signal_frame(bars: list[Bar] | pd.DataFrame) -> pd.DataFrame:
    """Signal bars as the rule baselines read them: mid OHLC indexed by ``available_at``.

    Accepts the event engine's `Bar` objects or a DATA-008 bar frame.
    """
    if isinstance(bars, pd.DataFrame):
        return pd.DataFrame(
            {
                part: bars[f"mid_{part}"].to_numpy("float64")
                for part in ("open", "high", "low", "close")
            },
            index=pd.DatetimeIndex(
                pd.to_datetime(bars["available_at_utc"].to_numpy("int64"), unit="ns", utc=True),
                name="available_at",
            ),
        )
    return pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
        },
        index=pd.DatetimeIndex([from_ns(b.available_at) for b in bars], name="available_at"),
    )


class ForecastSource(Protocol):
    """Forecasts made at the decision time from the bars seen so far (a model, or a test stub)."""

    def __call__(self, bar: Bar, ctx: StrategyContext) -> list[Forecast]:
        """The forecasts at ``ctx.now``."""
        ...


class RegimeSource(Protocol):
    """The filtered regime at the decision time (none exists before REG-007)."""

    def __call__(self, bar: Bar, ctx: StrategyContext) -> RegimeState | None:
        """The regime at ``ctx.now``."""
        ...


class SignalStrategy(Strategy):
    """The signal engine as an event-backtest strategy (module docstring)."""

    def __init__(
        self,
        engine: SignalEngine,
        forecasts: ForecastSource,
        *,
        regimes: RegimeSource | None = None,
    ) -> None:
        self.engine = engine
        self.source = forecasts
        self.regimes = regimes
        self.strategy_id = engine.spec.strategy_id
        self.version = engine.spec.version
        self.forecasts: list[Forecast] = []
        self.records: list[SignalRecord] = []

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[TradeIntent]:
        """The engine's intents on this bar's forecasts; the bar's spread is observed after."""
        forecasts = self.source(bar, ctx)
        self.forecasts.extend(forecasts)
        intents: list[TradeIntent] = []
        if ctx.quote is not None:
            decision = self.engine.decide(
                ctx.now,
                forecasts,
                bid=ctx.quote.bid,
                ask=ctx.quote.ask,
                sigma_daily=ctx.sigma_daily,
                regime=None if self.regimes is None else self.regimes(bar, ctx),
                position_lots=ctx.account.position_lots,
            )
            self.records.extend(decision.records)
            intents = decision.intents
        self.engine.observe_spread(ctx.now, bar.ask_close - bar.bid_close)
        return intents

    def audit(self, ledger: pd.DataFrame) -> pd.DataFrame:
        """Every fill of `ledger` traced to its order, decision, intent, record and forecasts.

        One row per fill: the ids along the chain, the decision's approval and profile version,
        the intent's reason (non-empty for the engine's own exits: time stop, weekend, kill switch)
        and, for a signal's intent, the record's outcome and its forecasts' ids and calibration.
        """
        records = {r.record_id: r for r in self.records}
        forecasts = {f.forecast_id: f for f in self.forecasts}
        intents = ledger.loc[ledger["kind"] == "intent"].set_index("intent_id")
        decisions = ledger.loc[ledger["kind"] == "decision"].set_index("decision_id")
        orders = ledger.loc[ledger["kind"] == "order"].set_index("order_id")
        rows = []
        for fill in ledger.loc[ledger["kind"] == "fill"].to_dict("records"):
            intent = intents.loc[fill["intent_id"]]
            decision = decisions.loc[fill["decision_id"]]
            signal_id = json.loads(str(intent["detail"]))["signal_id"]
            record = records.get(signal_id) if signal_id is not None else None
            linked = [] if record is None else list(record.candidate.forecast_ids)
            rows.append(
                {
                    "fill_id": fill["fill_id"],
                    "role": fill["role"],
                    "order_id": fill["order_id"],
                    "order_known": fill["order_id"] in orders.index,
                    "decision_id": fill["decision_id"],
                    "approved": bool(decision["approved"]),
                    "config_version": json.loads(str(decision["detail"]))["config_version"],
                    "intent_id": fill["intent_id"],
                    "intent_reason": intent["reason"],
                    "signal_id": signal_id,
                    "record_outcome": None if record is None else record.outcome,
                    "forecast_ids": tuple(linked),
                    "forecasts_known": all(f in forecasts for f in linked),
                    "calibrated": all(forecasts[f].calibrated for f in linked if f in forecasts),
                }
            )
        return pd.DataFrame(rows)
