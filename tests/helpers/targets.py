"""A stub target kind for testing the target framework without a real target computation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from xq.core.config import TargetSetConfig
from xq.data.calendar import MarketClock
from xq.targets.base import Lookahead, TargetKind, TargetSpec, market_horizon


def stub_compute(
    spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock | None = None
) -> pd.DataFrame:
    """Value = horizon in minutes; labels span exactly the horizon from the decision time."""
    index = pd.DatetimeIndex(sigma.index)
    return pd.DataFrame(
        {
            "value": np.full(len(index), spec.horizon / pd.Timedelta(minutes=1)),
            "label_start": index,
            "label_end": index + spec.horizon,
            "crosses_close": False,
            "scale": np.nan,
            "fill_delay_s": 0.0,
        },
        index=index,
    )


def _expand(definition: TargetSetConfig, trading_day: pd.Timedelta) -> list[TargetSpec]:
    return [
        TargetSpec(f"stub_{ref}_{h}", market_horizon(h, trading_day), ref, definition.params)
        for h in definition.horizons
        for ref in definition.price_refs
    ]


STUB_KIND = TargetKind(
    name="stub",
    code_version=1,
    expand=_expand,
    sigma=lambda close, definition, bar: pd.Series(1.0, index=close.index),
    compute=stub_compute,
    lookahead=lambda definition, trading_day: Lookahead(
        market=max(market_horizon(h, trading_day) for h in definition.horizons),
        wall=pd.Timedelta(0),
    ),
)
STUB_DEFINITION = TargetSetConfig(kind="stub", horizons=["15m", "1h"], price_refs=["long", "mid"])
#: The regular trading day of the repository calendar (18:00-17:00 New York).
TRADING_DAY = pd.Timedelta(hours=23)
