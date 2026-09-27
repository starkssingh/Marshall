"""Strategies shared by both backtest tiers (BT-009, BT-010).

The vectorized screener takes target exposures per decision time; these strategies express the
same decisions to the event engine as market-order intents, so a strategy can run through both
tiers and be reconciled:

- `ExposureStrategy` replays a series of target exposures decided in advance.
- `RuleStrategy` runs a BASE-002 rule baseline itself, bar by bar, on the signal bars it has seen
  so far (so it can only be causal), and targets the rule's latest exposure.

Both behave like the screener: an intent is sent when the target differs from the exposure of the
last target that was actually executed, so a missed, refused or expired decision is retried at
the next decision. The exposure is a request to the risk layer, which sizes it; they never size.
"""

from __future__ import annotations

import pandas as pd

from xq.backtest.engine import Strategy, StrategyContext
from xq.backtest.events import Bar, Fill
from xq.core.time import from_ns
from xq.models.baselines import RuleStrategyConfig, VolTargetConfig, rule_exposure
from xq.signals.schema import Direction, TradeIntent


class _TargetStrategy(Strategy):
    """Sends a market intent when the target differs from the last executed target."""

    def __init__(self) -> None:
        self.held = 0.0
        self._pending: float | None = None

    def _intents(self, target: float) -> list[TradeIntent]:
        self._pending = None
        if target == self.held:
            return []
        self._pending = target
        if target == 0:
            return [TradeIntent(direction="flat", strategy_id=self.strategy_id)]
        direction: Direction = "long" if target > 0 else "short"
        return [
            TradeIntent(direction=direction, exposure=abs(target), strategy_id=self.strategy_id)
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

    def __init__(self, positions: pd.Series, *, strategy_id: str = "exposure") -> None:
        super().__init__()
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
        return [] if target is None else self._intents(target)


class RuleStrategy(_TargetStrategy):
    """A BASE-002 rule baseline computed bar by bar on the signal bars seen so far."""

    def __init__(
        self,
        rule: RuleStrategyConfig,
        *,
        vol_target: VolTargetConfig | None = None,
        periods_per_year: int = 252,
        strategy_id: str | None = None,
    ) -> None:
        super().__init__()
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
        return self._intents(float(exposure.iloc[-1]))


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
