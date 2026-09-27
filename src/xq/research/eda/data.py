"""EDA inputs: the dataset's bars on the discovery window, and their returns (EDA-001).

`load_eda_inputs` reads a dataset's manifest (verifying its files) for the source, instrument, bar
build, price basis, window and the trading days the quality gate excluded, and loads the complete
bars of every requested timeframe through the catalog (which enforces the vault) restricted to the
discovery window: bars starting at or after its start and before the dataset's end, available at or
before its end, and not on an excluded trading day. `EdaInputs` checks every frame against the
window when it is built, so nothing later in the EDA can see post-discovery data.

`bar_returns` turns bars into close-to-close log returns. A return is kept only if its two bars are
adjacent in market time — no market-open time lies between the end of the first and the start of
the second (`MarketClock`) — so returns across the daily break, weekends and closed holidays are
kept (a holder bears them) and returns across missing or excluded bars are dropped. Each return
carries the interval it spans, ``[ret_start, ret_end)``: from the end of the previous bar (its
close) to the end of its own bar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import AppConfig, SessionsConfig
from xq.core.errors import NaiveTimestampError
from xq.core.time import TimestampLike, ensure_utc, trading_day
from xq.core.types import Timeframe
from xq.data.calendar import MarketClock, regular_trading_day
from xq.data.catalog import Catalog
from xq.datasets.builder import verify_dataset
from xq.datasets.spec import DatasetSpec
from xq.research.reports import DiscoveryWindow, DiscoveryWindowError, resolve_discovery_window

AVAILABLE = "available_at_utc"
BAR_START = "bar_start_utc"
_BPS = 1e4


@dataclass(frozen=True)
class EdaInputs:
    """A dataset's bars on the discovery window; every frame is checked against the window."""

    dataset_id: str
    spec: DatasetSpec
    window: DiscoveryWindow
    bars: Mapping[Timeframe, pd.DataFrame]
    excluded_days: frozenset[date]
    clock: MarketClock

    def __post_init__(self) -> None:
        for frame in self.bars.values():
            self.window.check(frame, available=AVAILABLE, start=BAR_START)

    def returns(self, tf: Timeframe) -> pd.DataFrame:
        """Adjacent close-to-close log returns of the `tf` bars (see `bar_returns`)."""
        return bar_returns(self.bars[tf], tf, self.clock)


def load_eda_inputs(
    cfg: AppConfig,
    dataset_id: str,
    timeframes: Sequence[Timeframe],
    *,
    end: TimestampLike | None = None,
) -> EdaInputs:
    """The dataset's complete bars of `timeframes` on its discovery window (module docstring).

    Args:
        end: Stop earlier than the discovery window's end; a later `end` is refused.

    Raises:
        DiscoveryWindowError: if `end` lies after the discovery window, or the dataset starts
            after the window ends.
        NoDatasetDataError, DatasetIntegrityError: if the dataset is missing or altered.
    """
    manifest = verify_dataset(cfg, dataset_id)
    spec = DatasetSpec.model_validate(manifest["spec"])
    window = resolve_discovery_window(cfg, spec.start)
    if end is not None:
        window = window.until(end)
    excluded = frozenset(
        date.fromisoformat(entry["trading_day"]) for entry in manifest["excluded_partitions"]
    )
    last_start = min(window.end, ensure_utc(spec.end))
    catalog = Catalog(cfg)
    bars: dict[Timeframe, pd.DataFrame] = {}
    for tf in dict.fromkeys(timeframes):
        frame = catalog.load_bars(
            spec.source,
            spec.instrument,
            tf,
            spec.price_basis,
            window.start,
            last_start,
            build=spec.bar_build,
        )
        keep = (
            frame["is_complete"].to_numpy(dtype=bool)
            & (frame[AVAILABLE] <= window.end).to_numpy()
            & ~frame["trading_day"].isin(excluded).to_numpy()
        )
        bars[tf] = frame.loc[keep].reset_index(drop=True)
    if all(frame.empty for frame in bars.values()):
        raise DiscoveryWindowError(
            f"dataset {dataset_id} has no complete bars in its discovery window "
            f"({window.start} to {window.end})"
        )
    clock = market_clock(cfg.sessions_config(), window.start, window.end)
    return EdaInputs(dataset_id, spec, window, bars, excluded, clock)


def market_clock(sessions: SessionsConfig, start: TimestampLike, end: TimestampLike) -> MarketClock:
    """A market clock covering every instant from `start` to `end` (a day of margin each side)."""
    first = trading_day(ensure_utc(start)) - timedelta(days=1)
    last = trading_day(ensure_utc(end)) + timedelta(days=1)
    return MarketClock.for_range(sessions, first, last)


def bar_returns(bars: pd.DataFrame, tf: Timeframe, clock: MarketClock) -> pd.DataFrame:
    """Close-to-close log returns of consecutive bars adjacent in market time.

    Returns:
        One row per kept return: ``bar_start`` (of the later bar), ``ret_start`` (the earlier
        bar's end), ``ret_end`` (the later bar's end), ``trading_day``, ``price`` (the earlier
        close, the entry price), ``close``, ``ret`` (log return) and the later bar's
        ``tick_count`` and ``spread_bps`` (mean spread over mid close, in basis points).
    """
    starts = instants_ns(bars[BAR_START])
    ends = starts + tf.nanos
    close = bars["close"].to_numpy(dtype=np.float64)
    if len(close) < 2:
        return _empty_returns()
    adjacent = clock.elapsed(ends[:-1]) == clock.elapsed(starts[1:])
    later = np.flatnonzero(adjacent) + 1
    earlier = later - 1
    frame = pd.DataFrame(
        {
            "bar_start": _utc(starts[later]),
            "ret_start": _utc(ends[earlier]),
            "ret_end": _utc(ends[later]),
            "trading_day": bars["trading_day"].to_numpy()[later],
            "price": close[earlier],
            "close": close[later],
            "ret": np.log(close[later] / close[earlier]),
            "tick_count": bars["tick_count"].to_numpy(dtype=np.int64)[later],
            "spread_bps": bars["spread_mean"].to_numpy(dtype=np.float64)[later]
            / close[later]
            * _BPS,
        }
    )
    return frame


def bars_per_trading_day(tf: Timeframe, sessions: SessionsConfig) -> int:
    """Bars of `tf` in one regular trading day (at least 1)."""
    return max(1, int(np.ceil(regular_trading_day(sessions) / tf.duration)))


def _empty_returns() -> pd.DataFrame:
    columns: dict[str, Any] = {
        "bar_start": pd.Series(dtype="datetime64[ns, UTC]"),
        "ret_start": pd.Series(dtype="datetime64[ns, UTC]"),
        "ret_end": pd.Series(dtype="datetime64[ns, UTC]"),
        "trading_day": pd.Series(dtype="object"),
        "price": pd.Series(dtype="float64"),
        "close": pd.Series(dtype="float64"),
        "ret": pd.Series(dtype="float64"),
        "tick_count": pd.Series(dtype="int64"),
        "spread_bps": pd.Series(dtype="float64"),
    }
    return pd.DataFrame(columns)


def instants_ns(column: pd.Series) -> npt.NDArray[np.int64]:
    """UTC int64 nanoseconds of a tz-aware timestamp column."""
    index = pd.DatetimeIndex(column)
    if index.tz is None:
        raise NaiveTimestampError("timestamps must be tz-aware")
    return index.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)


def _utc(ns: npt.NDArray[np.int64]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(ns, unit="ns", utc=True))
