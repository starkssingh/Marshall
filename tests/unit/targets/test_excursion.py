"""TGT-004: maximum favourable and adverse excursion in sigma units on synthetic paths, marked on
the exit side of the quote."""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import TargetSetConfig, load_config
from xq.core.errors import ConfigError
from xq.data.calendar import MarketClock
from xq.targets.base import TargetSpec
from xq.targets.excursion import compute, expand, segment_reduce

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 10), date(2024, 3, 22))
T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")
S = pd.Timedelta(seconds=1)
M = pd.Timedelta(minutes=1)
H = pd.Timedelta(hours=1)
PARAMS: dict[str, Any] = {
    "execution_latency_ms": 1000,
    "max_fill_delay_s": 300,
    "sigma_span_bars": 96,
}
SIGMA = 1e-3 / np.sqrt(60)  # sigma over 1 hour = 10 bp
SCALE = 1e-3


def spec(ref: str, measure: str) -> TargetSpec:
    return TargetSpec(f"tgt_{measure}_{ref}", H, ref, {**PARAMS, "measure": measure})  # type: ignore[arg-type]


def run(ref: str, measure: str, quotes: pd.DataFrame) -> pd.DataFrame:
    return compute(
        spec(ref, measure), quotes, pd.Series(SIGMA, index=pd.DatetimeIndex([T0])), CLOCK
    )


# A path: a quote before t0 + latency (never the entry), the entry at t0 + 2 s, a rise, a fall
# below the entry, and the exit fill just after t0 + 1 h + 1 s; then a quote after the window.
TIMES = [T0, T0 + 2 * S, T0 + 10 * M, T0 + 30 * M, T0 + 50 * M, T0 + H + 2 * S, T0 + 2 * H]
BID = [99.0, 100.0, 100.3, 99.6, 99.9, 100.1, 105.0]
SPREAD = [0.2, 0.1, 0.1, 0.4, 0.1, 0.2, 0.1]
QUOTES = pd.DataFrame(
    {"ts_utc": TIMES, "bid": BID, "ask": [b + s for b, s in zip(BID, SPREAD, strict=True)]}
)


@pytest.mark.parametrize(
    ("ref", "measure", "expected"),
    [
        # long: bought at the entry ask 100.1; the best bid on the path is 100.3, the worst 99.6
        ("long", "mfe", np.log(100.3 / 100.1)),
        ("long", "mae", -np.log(99.6 / 100.1)),
        # short: sold at the entry bid 100.0; the lowest ask is 100.0 (at 50 min), the highest 100.4
        ("short", "mfe", np.log(100.0 / 100.0)),
        ("short", "mae", -np.log(100.0 / 100.4)),
    ],
)
def test_excursions_on_the_exit_side_in_sigma_units(
    ref: str, measure: str, expected: float
) -> None:
    out = run(ref, measure, QUOTES)
    assert out["value"].iloc[0] == pytest.approx(expected / SCALE)
    assert out["scale"].iloc[0] == pytest.approx(SCALE)
    assert out["label_start"].iloc[0] == T0 + 2 * S
    assert out["label_end"].iloc[0] == T0 + H + 2 * S  # the 105.0 quote after the exit is not read


def test_a_steady_rise_has_no_adverse_excursion_beyond_the_spread() -> None:
    times = [T0 + 2 * S + k * 5 * M for k in range(13)]
    bid = 100.0 + 0.05 * np.arange(13)
    quotes = pd.DataFrame({"ts_utc": times, "bid": bid, "ask": bid + 0.1})
    mae = run("long", "mae", quotes)["value"].iloc[0] * SCALE
    mfe = run("long", "mfe", quotes)["value"].iloc[0] * SCALE
    assert mae == pytest.approx(-np.log(100.05 / 100.1))  # the first path bid against the ask
    assert mfe == pytest.approx(np.log(bid[-1] / 100.1))  # the exit fill is the best close-out


def test_quotes_while_closed_are_not_on_the_path() -> None:
    """A window across the 21:00 UTC close: a stray quote in the break is never marked."""
    t = pd.Timestamp("2024-03-12 20:30", tz="UTC")
    times = [t + 2 * S, t + 20 * M, pd.Timestamp("2024-03-12 21:30", tz="UTC"), t + 2 * H + S]
    quotes = pd.DataFrame({"ts_utc": times, "bid": [100.0, 100.2, 90.0, 100.1]})
    quotes["ask"] = quotes["bid"] + 0.1
    out = compute(spec("long", "mae"), quotes, pd.Series(SIGMA, index=pd.DatetimeIndex([t])), CLOCK)
    assert out["value"].iloc[0] * SCALE == pytest.approx(-np.log(100.1 / 100.1))
    assert out["crosses_close"].iloc[0]


def test_no_label_without_timely_fills_and_no_value_without_sigma() -> None:
    quotes = QUOTES.iloc[:5]  # no exit fill
    assert run("long", "mfe", quotes)["value"].isna().all()
    out = compute(
        spec("long", "mfe"), QUOTES, pd.Series(np.nan, index=pd.DatetimeIndex([T0])), CLOCK
    )
    assert out["value"].isna().all()


def test_segment_reduce_handles_overlapping_and_single_segments() -> None:
    values = np.array([3.0, 1.0, 4.0, 1.0, 5.0])
    first, last = np.array([0, 1, 4, 2]), np.array([2, 3, 4, 2])
    assert segment_reduce(np.maximum, values, first, last).tolist() == [4.0, 4.0, 5.0, 4.0]
    assert segment_reduce(np.minimum, values, first, last).tolist() == [1.0, 1.0, 5.0, 4.0]


def test_expansion_names_and_refusals() -> None:
    definition = TargetSetConfig(
        kind="excursion", horizons=["1h"], price_refs=["long", "short"], params=PARAMS
    )
    names = [s.name for s in expand(definition, pd.Timedelta(hours=23))]
    assert names == ["tgt_mfe_long_1h", "tgt_mae_long_1h", "tgt_mfe_short_1h", "tgt_mae_short_1h"]
    with pytest.raises(ConfigError, match="exit side"):
        expand(definition.model_copy(update={"price_refs": ["mid"]}), pd.Timedelta(hours=23))
    assert len(expand(CFG.target_set("excursions", "v1"), pd.Timedelta(hours=23))) == 16
