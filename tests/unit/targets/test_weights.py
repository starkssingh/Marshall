"""TGT-006: derived labels on hand-computed cases, and label concurrency and average-uniqueness
weights that match a hand example."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import TargetSetConfig, load_config
from xq.data.calendar import MarketClock
from xq.targets.weights import (
    compute,
    expand,
    financing_nights,
    label_uniqueness,
    uniqueness_weights,
)

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 10), date(2024, 3, 22))
T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")
S = pd.Timedelta(seconds=1)
H = pd.Timedelta(hours=1)
DEFINITION = CFG.target_set("derived", "v1")
SPECS = {s.name: s for s in expand(DEFINITION, pd.Timedelta(hours=23))}
SIGMA = 1e-3 / np.sqrt(60)  # 10 bp over the hour; 1.29 bp over one minute


# --- concurrency and uniqueness: the hand example ------------------------------------------------
#
#   A [0, 4)   B [2, 6)   C [5, 7)      concurrency: [0,2) 1, [2,4) 2, [4,5) 1, [5,6) 2, [6,7) 1
#   uniqueness A = (2 * 1 + 2 * 1/2) / 4 = 0.75
#              B = (2 * 1/2 + 1 * 1 + 1 * 1/2) / 4 = 0.625
#              C = (1 * 1/2 + 1 * 1) / 2 = 0.75
#   mean concurrency A = (2 + 4) / 4 = 1.5, B = (4 + 1 + 2) / 4 = 1.75, C = (2 + 1) / 2 = 1.5
#   weights = uniqueness * 3 / 2.125; weight_end A = 6 (B overlaps), B = 7, C = 7
EPOCH = pd.Timestamp("2024-03-12 10:00", tz="UTC")
HOUR = pd.Timedelta(hours=1)


def hours(*values: float) -> pd.Series:
    return pd.Series([EPOCH + v * HOUR for v in values], index=["A", "B", "C"][: len(values)])


def test_uniqueness_matches_the_hand_example() -> None:
    out = label_uniqueness(hours(0, 2, 5), hours(4, 6, 7))
    assert out["uniqueness"].tolist() == pytest.approx([0.75, 0.625, 0.75])
    assert out["concurrency"].tolist() == pytest.approx([1.5, 1.75, 1.5])
    assert list(out["weight_end"]) == [EPOCH + 6 * HOUR, EPOCH + 7 * HOUR, EPOCH + 7 * HOUR]
    weights = uniqueness_weights(out["uniqueness"])
    assert weights.tolist() == pytest.approx(
        [0.75 * 3 / 2.125, 0.625 * 3 / 2.125, 0.75 * 3 / 2.125]
    )
    assert weights.mean() == pytest.approx(1.0)
    assert list(out.index) == ["A", "B", "C"]


def test_identical_labels_share_their_weight_and_disjoint_labels_are_unique() -> None:
    same = label_uniqueness(hours(0, 0, 0), hours(2, 2, 2))
    assert same["uniqueness"].tolist() == pytest.approx([1 / 3] * 3)
    assert same["concurrency"].tolist() == pytest.approx([3.0] * 3)
    apart = label_uniqueness(hours(0, 2, 4), hours(1, 3, 5))
    assert apart["uniqueness"].tolist() == pytest.approx([1.0] * 3)
    assert list(apart["weight_end"]) == list(hours(1, 3, 5))
    touching = label_uniqueness(hours(0, 1), hours(1, 2))  # [0, 1) and [1, 2) do not overlap
    assert touching["uniqueness"].tolist() == pytest.approx([1.0, 1.0])


def test_rows_without_a_label_are_left_out_and_market_time_skips_the_break() -> None:
    start = pd.Series(
        [EPOCH, pd.NaT, EPOCH + HOUR], index=["A", "x", "B"], dtype="datetime64[ns, UTC]"
    )
    end = pd.Series(
        [EPOCH + 2 * HOUR, pd.NaT, EPOCH + 3 * HOUR],
        index=["A", "x", "B"],
        dtype="datetime64[ns, UTC]",
    )
    out = label_uniqueness(start, end)
    assert np.isnan(out.loc["x", "uniqueness"])
    assert pd.isna(out.loc["x", "weight_end"])
    assert out.loc["A", "uniqueness"] == pytest.approx(0.75)
    # over the daily break (21:00-22:00 UTC): in market time the overlap is 30 of 60 minutes,
    # in wall time 90 of 120
    a = pd.Series(pd.to_datetime(["2024-03-12 20:00", "2024-03-12 20:30"], utc=True))
    b = pd.Series(pd.to_datetime(["2024-03-12 22:00", "2024-03-12 22:30"], utc=True))
    wall = label_uniqueness(a, b)["uniqueness"].iloc[0]
    market = label_uniqueness(a, b, clock=CLOCK)["uniqueness"].iloc[0]
    assert wall == pytest.approx((30 + 90 / 2) / 120)
    assert market == pytest.approx((30 + 30 / 2) / 60)
    with pytest.raises(ValueError, match="ends before it starts"):
        label_uniqueness(hours(2), hours(1))


# --- derived labels -----------------------------------------------------------------------------

QUOTES = pd.DataFrame(
    {
        "ts_utc": [T0 + 2 * S, T0 + H + 2 * S],
        "bid": [2000.0, 2001.0],
        "ask": [2000.4, 2001.4],
    }
)


def run(name: str, quotes: pd.DataFrame = QUOTES, t: pd.Timestamp = T0) -> pd.DataFrame:
    return compute(SPECS[name], quotes, pd.Series(SIGMA, index=pd.DatetimeIndex([t])), CLOCK)


def test_sign_and_big_move_of_the_mid_return() -> None:
    # the mid goes from 2000.2 to 2001.2: +5.0 bp, below one sigma-hat (10 bp) over the hour
    assert run("tgt_sign_1h")["value"].iloc[0] == 1.0
    assert run("tgt_big_1h")["value"].iloc[0] == 0.0
    big = QUOTES.assign(bid=[2000.0, 2003.0], ask=[2000.4, 2003.4])  # 14.0 bp
    assert run("tgt_big_1h", big)["value"].iloc[0] == 1.0
    assert (
        run("tgt_sign_1h", QUOTES.assign(bid=[2000.0, 1999.0], ask=[2000.4, 1999.4]))["value"].iloc[
            0
        ]
        == -1.0
    )


def costs(mid: float, nights: float, rate_pct: float) -> float:
    """The placeholder round trip outside the spread, by hand."""
    commission = 2 * 0.035 / mid
    slippage = 2 * (0.5 + 0.1 * SIGMA / 1e-4) * 1e-4
    return commission + slippage + nights * rate_pct / 100 / 360


def test_trade_labels_net_of_the_round_trip_costs() -> None:
    # long: bought at 2000.4, sold at 2001.0: +3.0 bp, costs about 1.9 bp -> a trade
    long_r = np.log(2001.0 / 2000.4)
    assert long_r - costs(2000.2, 0, 6.0) > 0
    assert run("tgt_trade_long_1h")["value"].iloc[0] == 1.0
    # short: sold at 2000.0, bought back at 2001.4: a loss -> no trade
    assert run("tgt_trade_short_1h")["value"].iloc[0] == 0.0
    # a smaller gain that the spread covers but the rest of the costs do not
    thin = QUOTES.assign(bid=[2000.0, 2000.6], ask=[2000.4, 2001.0])  # +1.0 bp after the spread
    assert 0 < np.log(2000.6 / 2000.4) < costs(2000.2, 0, 6.0)
    assert run("tgt_trade_long_1h", thin)["value"].iloc[0] == 0.0


def test_financing_counts_three_nights_on_wednesday() -> None:
    closes = pd.to_datetime(CLOCK.closes, unit="ns", utc=True)
    tue, wed = (pd.Timestamp(f"2024-03-{d} 21:00", tz="UTC") for d in (12, 13))  # 17:00 New York
    assert tue in closes
    assert wed in closes
    start = np.array([pd.Timestamp("2024-03-12 20:00", tz="UTC").value])
    end = np.array([pd.Timestamp("2024-03-14 20:00", tz="UTC").value])
    assert financing_nights(CLOCK, start, end, "wed").tolist() == [1.0 + 3.0]
    # a 1d long held over Wednesday's close pays three nights
    t = pd.Timestamp("2024-03-13 20:00", tz="UTC")
    quotes = pd.DataFrame(
        {
            "ts_utc": [t + 2 * S, pd.Timestamp("2024-03-14 20:00:02", tz="UTC")],
            "bid": [2000.0, 2001.2],
            "ask": [2000.4, 2001.6],
        }
    )
    gain = np.log(2001.2 / 2000.4)  # +4.0 bp: beats one night (about 3.6 bp in all), not three
    sigma = pd.Series(SIGMA, index=pd.DatetimeIndex([t]))
    out = compute(SPECS["tgt_trade_long_1d"], quotes, sigma, CLOCK)
    assert gain - costs(2000.2, 1, 6.0) > 0 > gain - costs(2000.2, 3, 6.0)
    assert out["value"].iloc[0] == 0.0
    assert out["crosses_close"].iloc[0]


def test_the_costs_mirror_the_placeholder_cost_model() -> None:
    """The trade label's costs are the placeholder model's (config/costs/placeholder.yaml)."""
    p = DEFINITION.params
    model = CFG.cost_model_config("placeholder")
    instrument = CFG.instrument("xauusd")
    assert p["commission_usd_per_oz_per_side"] == pytest.approx(
        model.commission.per_lot_per_side_usd / float(instrument.contract_size)
    )
    assert model.commission.per_notional_per_side_bps == 0
    assert p["slippage_fixed_bps"] == model.slippage.fixed_bps
    assert p["slippage_sigma_multiple"] == model.slippage.sigma_multiple
    assert p["financing_long_annual_pct"] == model.financing.long_rate_annual_pct
    assert p["financing_short_annual_pct"] == model.financing.short_rate_annual_pct
    assert p["financing_day_count"] == model.financing.day_count
    assert p["triple_weekday"] == model.financing.triple_weekday
    assert p["execution_latency_ms"] == model.latency_ms
    assert p["max_fill_delay_s"] == model.max_fill_delay_s


def test_expansion_by_price_reference() -> None:
    definition = TargetSetConfig(
        kind="derived_label", horizons=["1h"], price_refs=["long", "mid"], params=DEFINITION.params
    )
    names = [s.name for s in expand(definition, pd.Timedelta(hours=23))]
    assert names == ["tgt_trade_long_1h", "tgt_sign_1h", "tgt_big_1h"]
    assert len(SPECS) == 16


def test_no_label_without_fills_or_sigma() -> None:
    assert run("tgt_trade_long_1h", QUOTES.iloc[:1])["value"].isna().all()
    out = compute(
        SPECS["tgt_big_1h"], QUOTES, pd.Series(np.nan, index=pd.DatetimeIndex([T0])), CLOCK
    )
    assert out["value"].isna().all()
    assert run("tgt_sign_1h", t=pd.Timestamp("2024-03-16 12:00", tz="UTC"))["value"].isna().all()
