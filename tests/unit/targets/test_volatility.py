"""TGT-003: future realized volatility matches the realized volatility computed separately, on
synthetic quote paths (trading-time grid, ADR 0026)."""

from datetime import date
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import TargetSetConfig, load_config
from xq.core.errors import ConfigError
from xq.data.calendar import MarketClock
from xq.targets.base import TargetSpec
from xq.targets.volatility import compute, expand

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 10), date(2024, 3, 22))
T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")  # Tuesday, market open
S = pd.Timedelta(seconds=1)
M = pd.Timedelta(minutes=1)
H = pd.Timedelta(hours=1)
PARAMS: dict[str, Any] = {
    "execution_latency_ms": 1000,
    "max_fill_delay_s": 300,
    "sigma_span_bars": 96,
    "sample_minutes": 5,
    "vol_normalized": True,
}


def spec(horizon: pd.Timedelta = H, *, normalized: bool = False) -> TargetSpec:
    return TargetSpec("tgt_rv", horizon, "mid", {**PARAMS, "normalized": normalized})


def path(start: pd.Timestamp, end: pd.Timestamp, *, every: pd.Timedelta, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    stamps = pd.date_range(start, end, freq=every)
    mid = 2000 * np.exp(np.cumsum(rng.normal(0, 4e-4, len(stamps))))
    return pd.DataFrame({"ts_utc": stamps, "bid": mid - 0.1, "ask": mid + 0.1})


def separately(quotes: pd.DataFrame, grid: list[pd.Timestamp], entry: int, exit_: int) -> float:
    """Realized volatility with a plain loop: entry mid, the last mid at or before each grid
    point (not before the entry), the exit mid."""
    mids = ((quotes["bid"] + quotes["ask"]) / 2).to_numpy()
    times = list(quotes["ts_utc"])
    points = [mids[entry]]
    for g in grid:
        last = max(i for i, ts in enumerate(times) if ts <= g)
        points.append(mids[max(last, entry)])
    points.append(mids[exit_])
    return float(np.sqrt(sum(np.log(b / a) ** 2 for a, b in pairwise(points))))


def test_matches_the_realized_volatility_computed_separately() -> None:
    quotes = path(T0 - 5 * M, T0 + 2 * H, every=37 * S, seed=1)
    out = compute(spec(), quotes, pd.Series(1e-4, index=pd.DatetimeIndex([T0])), CLOCK)
    times = pd.DatetimeIndex(quotes["ts_utc"])
    entry = int(times.searchsorted(T0 + S))  # the first quote at or after t + latency
    exit_ = int(times.searchsorted(T0 + H + S))
    grid = [T0 + S + k * 5 * M for k in range(1, 12)]
    assert out["value"].iloc[0] == pytest.approx(separately(quotes, grid, entry, exit_))
    assert out["label_start"].iloc[0] == times[entry]
    assert out["label_end"].iloc[0] == times[exit_]
    assert not out["crosses_close"].iloc[0]


def test_a_gap_in_quotes_carries_the_last_mid_forward() -> None:
    quotes = path(T0 - 5 * M, T0 + 2 * H, every=30 * S, seed=2)
    times = pd.DatetimeIndex(quotes["ts_utc"])
    gap = (times > T0 + 10 * M) & (times < T0 + 40 * M)  # no quote for half an hour
    quotes = quotes.loc[~gap].reset_index(drop=True)
    times = pd.DatetimeIndex(quotes["ts_utc"])
    out = compute(spec(), quotes, pd.Series(1e-4, index=pd.DatetimeIndex([T0])), CLOCK)
    grid = [T0 + S + k * 5 * M for k in range(1, 12)]
    entry, exit_ = int(times.searchsorted(T0 + S)), int(times.searchsorted(T0 + H + S))
    assert out["value"].iloc[0] == pytest.approx(separately(quotes, grid, entry, exit_))


def test_a_window_across_the_daily_close_is_sampled_in_market_time() -> None:
    """Decided 30 minutes before the 21:00 UTC close (17:00 New York, EDT): the grid stops at
    the close and resumes at the 22:00 UTC open, so the first point after it measures the gap."""
    t = pd.Timestamp("2024-03-12 20:30", tz="UTC")
    before = path(t - 5 * M, pd.Timestamp("2024-03-12 20:59:30", tz="UTC"), every=30 * S, seed=3)
    after = path(pd.Timestamp("2024-03-12 22:00:00.5", tz="UTC"), t + 3 * H, every=30 * S, seed=4)
    after[["bid", "ask"]] *= 1.01  # a 1 % gap over the break
    quotes = pd.concat([before, after], ignore_index=True)
    out = compute(spec(), quotes, pd.Series(1e-4, index=pd.DatetimeIndex([t])), CLOCK)
    times = pd.DatetimeIndex(quotes["ts_utc"])
    opened = pd.Timestamp("2024-03-12 22:00", tz="UTC")
    grid = [t + S + k * 5 * M for k in range(1, 6)]  # 20:35:01 ... 20:55:01
    grid += [opened + S + k * 5 * M for k in range(6)]  # 22:00:01 ... 22:25:01
    entry = int(times.searchsorted(t + S))
    exit_ = int(times.searchsorted(opened + 30 * M + S))
    expected = separately(quotes, grid, entry, exit_)
    assert out["value"].iloc[0] == pytest.approx(expected)
    assert expected > 0.0099  # the overnight gap is in the window
    assert out["crosses_close"].iloc[0]
    assert out["label_end"].iloc[0] == times[exit_]


def test_the_normalized_variant_divides_by_sigma_over_the_horizon() -> None:
    quotes = path(T0 - 5 * M, T0 + 2 * H, every=40 * S, seed=5)
    sigma = pd.Series(2e-4, index=pd.DatetimeIndex([T0]))
    raw = compute(spec(), quotes, sigma, CLOCK)
    vol = compute(spec(normalized=True), quotes, sigma, CLOCK)
    assert vol["scale"].iloc[0] == pytest.approx(2e-4 * np.sqrt(60))
    assert vol["value"].iloc[0] == pytest.approx(raw["value"].iloc[0] / (2e-4 * np.sqrt(60)))


def test_no_label_while_closed_or_without_timely_fills() -> None:
    quotes = path(T0 - 5 * M, T0 + 30 * M, every=30 * S, seed=6)  # ends before the exit
    closed = pd.Timestamp("2024-03-16 12:00", tz="UTC")  # Saturday
    sigma = pd.Series(1e-4, index=pd.DatetimeIndex([T0, closed]))
    out = compute(spec(), quotes, sigma, CLOCK)
    assert out["value"].isna().all()
    assert out["label_end"].isna().all()


def test_expansion_names_and_refusals() -> None:
    definition = TargetSetConfig(
        kind="realized_vol", horizons=["15m", "1d"], price_refs=["mid"], params=PARAMS
    )
    specs = expand(definition, pd.Timedelta(hours=23))
    assert [s.name for s in specs] == ["tgt_rv_15m", "tgt_rv_15m_vol", "tgt_rv_1d", "tgt_rv_1d_vol"]
    assert specs[2].horizon == pd.Timedelta(hours=23)
    with pytest.raises(ConfigError, match="price_refs must be"):
        expand(definition.model_copy(update={"price_refs": ["long"]}), pd.Timedelta(hours=23))
    with pytest.raises(ConfigError, match="whole number"):
        expand(definition.model_copy(update={"horizons": ["7m"]}), pd.Timedelta(hours=23))
    with pytest.raises(ConfigError, match="invalid realized_vol params"):
        expand(definition.model_copy(update={"params": {}}), pd.Timedelta(hours=23))


def test_the_repository_target_set_expands() -> None:
    definition = CFG.target_set("realized_vol", "v1")
    names = [s.name for s in expand(definition, pd.Timedelta(hours=23))]
    assert names == [f"tgt_rv_{h}{v}" for h in ("15m", "1h", "4h", "1d") for v in ("", "_vol")]
