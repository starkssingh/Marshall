"""Shared pieces for event-backtester tests: exact test costs, synthetic quotes, scripted
strategies and a recorder that keeps every call."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from helpers.pipeline import REPO
from xq.backtest.costs import CostModel
from xq.backtest.engine import Strategy, StrategyContext
from xq.backtest.events import Bar, Fill
from xq.core.config import CostModelConfig, load_config
from xq.core.time import to_ns
from xq.data.calendar import MarketClock
from xq.signals.schema import TradeIntent

CFG = load_config("research", config_dir=REPO / "config")
SESSIONS = CFG.sessions_config()
INSTRUMENT = CFG.instrument("xauusd")
CLOCK = MarketClock.for_range(SESSIONS, date(2024, 2, 20), date(2024, 4, 15))
CAPITAL = 100_000.0
MINUTE_NS = 60_000_000_000


def exact_costs(
    *,
    long_rate: float = 3.6,
    short_rate: float = 3.6,
    slippage_bps: float = 0.5,
    multipliers: Mapping[str, float] | None = None,
) -> CostModel:
    """Exact costs: 3.5 USD per lot per side, fixed slippage, act/360 financing, Wednesday x3."""
    return CostModel(
        CostModelConfig(
            venue="test",
            provisional=True,
            latency_ms=1000,
            max_fill_delay_s=300,
            commission={"per_lot_per_side_usd": 3.5},  # type: ignore[arg-type]
            slippage={  # type: ignore[arg-type]
                "fixed_bps": slippage_bps,
                "sigma_multiple": 0.0,
                "multipliers": dict(multipliers or {}),
            },
            financing={  # type: ignore[arg-type]
                "long_rate_annual_pct": long_rate,
                "short_rate_annual_pct": short_rate,
            },
        ),
        INSTRUMENT,
        SESSIONS,
    )


def ns(text: str) -> int:
    return to_ns(pd.Timestamp(text, tz="UTC"))


def quotes(*rows: tuple[str, float, float]) -> pd.DataFrame:
    """Quotes from (UTC time, bid, ask) rows."""
    return pd.DataFrame(
        {
            "ts_utc": [pd.Timestamp(t, tz="UTC") for t, _, _ in rows],
            "bid": [b for _, b, _ in rows],
            "ask": [a for _, _, a in rows],
        }
    )


def random_quotes(
    start: str,
    end: str,
    *,
    seed: int,
    every_s: float = 20.0,
    price: float = 2000.0,
    step: float = 0.15,
    spread: tuple[float, float] = (0.1, 0.4),
) -> pd.DataFrame:
    """Random-walk quotes in ``[start, end)`` while the market is open, jittered times."""
    rng = np.random.default_rng(seed)
    grid = pd.date_range(start, end, freq=f"{every_s}s", tz="UTC", inclusive="left")
    t = grid.as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    t = t + rng.integers(0, int(every_s * 1e9) // 2, len(t))
    t = np.sort(t[CLOCK.is_open(t)])
    mid = price + np.cumsum(rng.normal(0, step, len(t)))
    half = rng.uniform(*spread, len(t)) / 2
    return pd.DataFrame(
        {"ts_utc": pd.to_datetime(t, unit="ns", utc=True), "bid": mid - half, "ask": mid + half}
    )


class ScriptedStrategy(Strategy):
    """Emits given intents at given decision times (bar availabilities)."""

    strategy_id = "scripted"

    def __init__(self, script: Mapping[str, Sequence[TradeIntent]]) -> None:
        self.script = {ns(t): list(intents) for t, intents in script.items()}
        self.fills: list[Fill] = []

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[TradeIntent]:
        return self.script.get(ctx.now, [])

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        self.fills.append(fill)


class RandomStrategy(Strategy):
    """Random intents on some bars: market and bracket entries, flips and exits (seeded)."""

    strategy_id = "random"

    def __init__(self, seed: int, *, trade_probability: float = 0.3, brackets: bool = True) -> None:
        self.rng = np.random.default_rng(seed)
        self.p = trade_probability
        self.brackets = brackets

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[TradeIntent]:
        if self.rng.random() >= self.p:
            return []
        choice = self.rng.integers(0, 3)
        if choice == 0:
            return [TradeIntent(direction="flat", strategy_id=self.strategy_id)]
        direction = "long" if choice == 1 else "short"
        exposure = float(self.rng.choice([0.25, 0.5, 1.0]))
        stop = target = None
        if self.brackets and self.rng.random() < 0.5:
            distance = bar.close * float(self.rng.uniform(5, 20)) * 1e-4  # 5-20 bp
            sign = 1 if direction == "long" else -1
            stop, target = bar.close - sign * distance, bar.close + sign * 1.5 * distance
        return [
            TradeIntent(
                direction=direction,
                exposure=exposure,
                stop=stop,
                target=target,
                strategy_id=self.strategy_id,
            )
        ]


class CallRecorder:
    """Keeps every recorder call (engine and broker side)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, {"args": args, **kwargs}))

        return record

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]
