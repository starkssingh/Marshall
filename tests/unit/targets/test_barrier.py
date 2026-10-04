"""TGT-005: triple-barrier labels recover known hit times on synthetic paths; a bar touching both
barriers resolves pessimistically to the stop and is flagged; ticks resolve it exactly."""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import TargetSetConfig, load_config
from xq.core.errors import ConfigError
from xq.data.calendar import MarketClock
from xq.targets.barrier import compute, expand

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 10), date(2024, 3, 22))
T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")  # Tuesday, market open
S = pd.Timedelta(seconds=1)
M = pd.Timedelta(minutes=1)
H = pd.Timedelta(hours=1)
SCALE = 1e-3  # sigma-hat over the 1-hour horizon: 10 bp
SIGMA = SCALE / np.sqrt(60)
PARAMS: dict[str, Any] = {
    "execution_latency_ms": 1000,
    "max_fill_delay_s": 300,
    "sigma_span_bars": 96,
    "tp_sigmas": 1.0,
    "sl_sigmas": 1.0,
    "resolution": "tick",
}
SPREAD = 0.05


def specs(ref: str = "long", **params: Any) -> dict[str, Any]:
    definition = TargetSetConfig(
        kind="triple_barrier", horizons=["1h"], price_refs=[ref], params={**PARAMS, **params}
    )
    return {s.params["measure"]: s for s in expand(definition, pd.Timedelta(hours=23))}


def run(quotes: pd.DataFrame, decisions: list[pd.Timestamp], ref: str = "long", **params: Any):  # type: ignore[no-untyped-def]
    sigma = pd.Series(SIGMA, index=pd.DatetimeIndex(decisions))
    return {m: compute(s, quotes, sigma, CLOCK) for m, s in specs(ref, **params).items()}


def quotes_of(times: list[pd.Timestamp], bids: list[float]) -> pd.DataFrame:
    bid = np.array(bids, dtype=np.float64)
    return pd.DataFrame({"ts_utc": times, "bid": bid, "ask": bid + SPREAD})


def ramp(target_bid: float, at: pd.Timedelta) -> pd.DataFrame:
    """Quotes every 30 s from t0 + 2 s, flat at 100 except one move to `target_bid` at `at`."""
    times = [T0 + 2 * S + k * 30 * S for k in range(150)]
    bids = [target_bid if abs(t - (T0 + at)) < 15 * S else 100.0 for t in times]
    return quotes_of(times, bids)


# long: entry ask 100.05; take-profit when a bid reaches 100.05 e^0.001 = 100.150,
# stop when a bid falls to 100.05 e^-0.001 = 99.950
@pytest.mark.parametrize(("bid", "label"), [(100.16, 1.0), (99.94, -1.0)])
def test_a_known_hit_time_is_recovered(bid: float, label: float) -> None:
    hit_at = T0 + 2 * S + 40 * 30 * S  # the 41st quote, 20 minutes after the entry
    out = run(ramp(bid, hit_at - T0), [T0])
    assert out["label"]["value"].iloc[0] == label
    assert out["label"]["label_start"].iloc[0] == T0 + 2 * S
    assert out["label"]["label_end"].iloc[0] == hit_at
    assert out["time"]["value"].iloc[0] == pytest.approx(20.0)
    assert out["ambiguous"]["value"].iloc[0] == 0.0
    assert out["time"]["label_end"].iloc[0] == hit_at  # the three targets share the window


def test_the_vertical_barrier_labels_zero_at_the_exit_fill() -> None:
    out = run(ramp(100.1, 30 * M), [T0])  # 100.1 is inside both barriers
    assert out["label"]["value"].iloc[0] == 0.0
    exit_fill = T0 + 2 * S + 120 * 30 * S  # 11:00:02, the first quote at or after t0 + 1 h + 1 s
    assert out["label"]["label_end"].iloc[0] == exit_fill
    assert out["time"]["value"].iloc[0] == pytest.approx((exit_fill - (T0 + 2 * S)) / M)


def test_the_short_side_reads_the_ask() -> None:
    # short: entry bid 100.00; take-profit when the ask falls to 100 e^-0.001 = 99.900
    out = run(ramp(99.84, 10 * M), [T0], ref="short")  # ask 99.89
    assert out["label"]["value"].iloc[0] == 1.0
    out = run(ramp(100.06, 10 * M), [T0], ref="short")  # ask 100.11 >= 100 e^0.001 = 100.100
    assert out["label"]["value"].iloc[0] == -1.0


def brute_force(quotes: pd.DataFrame, t: pd.Timestamp) -> tuple[float, pd.Timestamp]:
    """The first quote after the entry fill crossing a barrier, by a plain loop (long)."""
    times = list(quotes["ts_utc"])
    entry = next(i for i, ts in enumerate(times) if ts >= t + S)
    exit_ = next(i for i, ts in enumerate(times) if ts >= t + H + S)
    ask_entry = quotes["ask"].iloc[entry]
    for i in range(entry + 1, exit_ + 1):
        gain = np.log(quotes["bid"].iloc[i] / ask_entry)
        if gain >= SCALE:
            return 1.0, times[i]
        if gain <= -SCALE:
            return -1.0, times[i]
    return 0.0, times[exit_]


def test_hit_times_on_random_paths_match_a_brute_force_search() -> None:
    rng = np.random.default_rng(11)
    times = list(pd.date_range(T0 - 5 * M, T0 + 5 * H, freq="20s"))
    bids = 2000 * np.exp(np.cumsum(rng.normal(0, 2.5e-4, len(times))))
    quotes = pd.DataFrame({"ts_utc": times, "bid": bids, "ask": bids + 0.2})
    decisions = list(pd.date_range(T0, T0 + 3 * H, freq="15min"))
    out = run(quotes, decisions)["label"]
    labels = []
    for t in decisions:
        label, end = brute_force(quotes, t)
        assert out.loc[t, "value"] == label
        assert out.loc[t, "label_end"] == end
        labels.append(label)
    assert {-1.0, 1.0} <= set(labels)  # both barriers are exercised


def same_bar_path(first: float, second: float) -> pd.DataFrame:
    """Flat at 100, then inside the 10:10-10:15 bar a move to `first` and then to `second`."""
    times = [T0 + 2 * S + k * 30 * S for k in range(150)]
    bids = [100.0] * len(times)
    bids[20] = first  # 10:10:02
    bids[22] = second  # 10:11:02
    return quotes_of(times, bids)


@pytest.mark.parametrize(("first", "second"), [(100.16, 99.94), (99.94, 100.16)])
def test_a_bar_touching_both_barriers_is_a_flagged_stop(first: float, second: float) -> None:
    out = run(same_bar_path(first, second), [T0], resolution="5min")
    assert out["label"]["value"].iloc[0] == -1.0  # pessimistic, whichever came first
    assert out["ambiguous"]["value"].iloc[0] == 1.0
    bar_end = T0 + 2 * S + 29 * 30 * S  # the bar's last quote, 10:14:32
    assert out["label"]["label_end"].iloc[0] == bar_end
    # ticks resolve the same path exactly, never ambiguous
    ticks = run(same_bar_path(first, second), [T0])
    assert ticks["label"]["value"].iloc[0] == (1.0 if first > 100 else -1.0)
    assert ticks["ambiguous"]["value"].iloc[0] == 0.0
    assert ticks["label"]["label_end"].iloc[0] == T0 + 2 * S + 20 * 30 * S


def test_a_bar_touching_one_barrier_is_not_ambiguous() -> None:
    out = run(ramp(100.16, 20 * M), [T0], resolution="5min")
    assert out["label"]["value"].iloc[0] == 1.0
    assert out["ambiguous"]["value"].iloc[0] == 0.0


def test_a_hit_labels_the_window_without_its_exit_fill() -> None:
    hit = ramp(100.16, 20 * M)
    cut = hit.loc[hit["ts_utc"] <= T0 + 30 * M]  # the data ends before the vertical barrier
    out = run(cut, [T0])
    assert out["label"]["value"].iloc[0] == 1.0
    assert out["label"]["fill_delay_s"].iloc[0] == pytest.approx(1.0)  # the entry fill's delay
    flat = ramp(100.0, 20 * M)
    assert run(flat.loc[flat["ts_utc"] <= T0 + 30 * M], [T0])["label"]["value"].isna().all()


def test_a_weekend_gap_through_the_stop_hits_at_the_first_quote_after_the_open() -> None:
    t = pd.Timestamp("2024-03-15 20:30", tz="UTC")  # Friday, 30 min before the 21:00 UTC close
    friday = [t + 2 * S + k * 60 * S for k in range(28)]
    sunday = [pd.Timestamp("2024-03-17 22:00:30", tz="UTC") + k * 60 * S for k in range(60)]
    quotes = quotes_of(friday + sunday, [100.0] * len(friday) + [99.0] * len(sunday))
    out = run(quotes, [t])
    assert out["label"]["value"].iloc[0] == -1.0
    assert out["label"]["label_end"].iloc[0] == sunday[0]
    assert out["label"]["crosses_close"].iloc[0]
    assert out["time"]["value"].iloc[0] == pytest.approx((30 * M - 2 * S + 30 * S) / M)


def test_no_label_while_closed_or_without_sigma() -> None:
    quotes = ramp(100.16, 20 * M)
    closed = pd.Timestamp("2024-03-16 12:00", tz="UTC")
    assert run(quotes, [closed])["label"]["value"].isna().all()
    sigma = pd.Series(np.nan, index=pd.DatetimeIndex([T0]))
    assert compute(specs()["label"], quotes, sigma, CLOCK)["value"].isna().all()


def test_expansion_names_and_refusals() -> None:
    names = [s.name for s in specs().values()]
    assert names == ["tgt_tb_long_1h", "tgt_tb_long_1h_t", "tgt_tb_long_1h_amb"]
    with pytest.raises(ConfigError, match="side-specific"):
        specs("mid")
    with pytest.raises(ConfigError, match="resolution"):
        specs(resolution="often")
    with pytest.raises(ConfigError, match="tp_sigmas"):
        specs(tp_sigmas=0)
    assert len(expand(CFG.target_set("barriers", "v1"), pd.Timedelta(hours=23))) == 24
