"""DATA-008 properties: OHLC sanity, containment, availability and timeframe consistency."""

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from xq.core.types import Timeframe
from xq.data.adapters.base import TICK_SCHEMA
from xq.data.bars import BASES, build_bars
from xq.data.flags import TickFlag

EXCLUDE = int(TickFlag.CROSSED | TickFlag.MISSING_QUOTE)
START = int(pd.Timestamp("2024-03-08 18:00", tz="UTC").value)  # spans the weekend and DST change
LATENCY = 100_000_000


@st.composite
def tick_frames(draw: st.DrawFn) -> pd.DataFrame:
    """Hypothesis picks the shape (size, gap scale, flag mix, seed); numpy fills the arrays."""
    n = draw(st.integers(min_value=1, max_value=300))
    max_gap_ms = draw(st.sampled_from([1, 2_000, 90_000, 3 * 3600 * 1000]))
    crossed_share = draw(st.sampled_from([0.0, 0.1, 0.5]))
    rng = np.random.default_rng(draw(st.integers(min_value=0, max_value=2**32 - 1)))
    ts = START + np.cumsum(rng.integers(0, max_gap_ms + 1, n)) * 1_000_000
    bid = np.round(2000 + rng.normal(0, 5, n), 2)
    ask = bid + np.round(rng.uniform(0, 2, n), 2)
    flags = np.where(rng.random(n) < 0.2, np.uint32(TickFlag.STALE), np.uint32(0))
    crossed = rng.random(n) < crossed_share
    flags[crossed] = np.uint32(TickFlag.CROSSED)
    ask[crossed] = bid[crossed] - 1.0  # crossed ticks really are crossed
    return pd.DataFrame(
        {
            "ts_utc": ts,
            "bid": bid,
            "ask": ask,
            "bid_size": np.nan,
            "ask_size": np.nan,
            "flags": flags.astype(np.uint32),
            "raw_file_id": "r",
            "row_num": np.arange(n),
        }
    ).astype(TICK_SCHEMA)


def bars_for(frame: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    return build_bars(
        frame,
        tf,
        exclude_flags=EXCLUDE,
        latency_ns=LATENCY,
        coverage_end_ns=int(frame["ts_utc"].max()),
    )


def aggregate(fine: pd.DataFrame, coarse: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    """Aggregate finer bars into the buckets of `coarse` (built from ticks at timeframe `tf`)."""
    edges = coarse["bar_start_utc"].to_numpy()
    owner = np.searchsorted(edges, fine["bar_start_utc"].to_numpy(), side="right") - 1
    grouped = fine.assign(owner=owner, spread_sum=fine["spread_mean"] * fine["tick_count"])
    grouped = grouped.groupby("owner", sort=True)
    out = pd.DataFrame({"bar_start_utc": edges})
    for basis in BASES:
        out[f"{basis}_open"] = grouped[f"{basis}_open"].first().to_numpy()
        out[f"{basis}_high"] = grouped[f"{basis}_high"].max().to_numpy()
        out[f"{basis}_low"] = grouped[f"{basis}_low"].min().to_numpy()
        out[f"{basis}_close"] = grouped[f"{basis}_close"].last().to_numpy()
    for column in ("tick_count", "n_flagged", "n_excluded"):
        out[column] = grouped[column].sum().to_numpy()
    out["trading_day"] = grouped["trading_day"].first().to_numpy()
    out["spread_max"] = grouped["spread_max"].max().to_numpy()
    out["spread_close"] = grouped["spread_close"].last().to_numpy()
    out["spread_sum"] = grouped["spread_sum"].sum().to_numpy()
    return out


@settings(max_examples=40, deadline=None)
@given(tick_frames())
def test_ohlc_sanity_and_no_crossed_close(frame: pd.DataFrame) -> None:
    for tf in Timeframe:
        bars = bars_for(frame, tf)
        for basis in BASES:
            o, h, lo, c = (bars[f"{basis}_{p}"] for p in ("open", "high", "low", "close"))
            assert (h >= np.maximum(o, c)).all()
            assert (lo <= np.minimum(o, c)).all()
        assert (bars["ask_close"] >= bars["bid_close"]).all()
        assert (bars["mid_high"] <= bars["ask_high"] + 1e-9).all()
        assert (bars["mid_low"] >= bars["bid_low"] - 1e-9).all()


@settings(max_examples=40, deadline=None)
@given(tick_frames())
def test_every_included_tick_is_in_exactly_one_bar_before_available_at(frame: pd.DataFrame) -> None:
    included = frame[(frame["flags"].to_numpy() & np.uint32(EXCLUDE)) == 0]
    ts = included["ts_utc"].to_numpy()
    for tf in Timeframe:
        bars = bars_for(frame, tf)
        assert bars["tick_count"].sum() == len(included)
        starts = bars["bar_start_utc"].to_numpy()
        available = bars["available_at_utc"].to_numpy()
        owner = np.searchsorted(starts, ts, side="right") - 1
        assert (owner >= 0).all()
        assert (ts >= starts[owner]).all()
        # No bar contains a tick at or after its available_at (or its end).
        assert (ts < available[owner] - LATENCY).all()
        assert (np.diff(starts) > 0).all()  # one bar per start, ordered
        assert bars["n_excluded"].sum() <= len(frame) - len(included)


@settings(max_examples=40, deadline=None)
@given(tick_frames())
def test_higher_timeframes_equal_aggregated_finer_bars(frame: pd.DataFrame) -> None:
    pairs = [
        (Timeframe.M1, tf) for tf in (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1)
    ]
    pairs += [(Timeframe.H1, Timeframe.H4), (Timeframe.H1, Timeframe.D1)]
    for fine_tf, coarse_tf in pairs:
        fine, coarse = bars_for(frame, fine_tf), bars_for(frame, coarse_tf)
        rebuilt = aggregate(fine, coarse, coarse_tf)
        # An excluded tick alone in a fine bucket produces no fine bar but is counted by the
        # coarse bar, so n_excluded is not additive: the coarse count can only be larger.
        assert (coarse["n_excluded"].to_numpy() >= rebuilt["n_excluded"].to_numpy()).all()
        for column in rebuilt.columns.drop("n_excluded"):
            if column == "spread_sum":
                expected = coarse["spread_mean"] * coarse["tick_count"]
                np.testing.assert_allclose(rebuilt[column], expected, rtol=1e-9, atol=1e-9)
            else:
                np.testing.assert_array_equal(
                    rebuilt[column].to_numpy(),
                    coarse[column].to_numpy(),
                    err_msg=f"{fine_tf}->{coarse_tf} {column}",
                )
