"""TGT-006: derived labels on hand-computed cases, and label concurrency and average-uniqueness
weights that match a hand example. C-30 (3), ADR 0065: the trade/no-trade label is priced by the
backtester's own cost model, session and event multipliers included."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.backtest.costs import CostModel
from xq.backtest.vectorized import run_vectorized
from xq.core.config import TargetSetConfig, load_config
from xq.core.errors import ConfigError
from xq.data.calendar import MarketClock
from xq.targets.kinds import target_specs
from xq.targets.returns import label_windows
from xq.targets.weights import (
    compute,
    expand,
    label_uniqueness,
    round_trip_pnl,
    uniqueness_weights,
)

CFG = load_config("research", config_dir=REPO / "config")
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 10), date(2024, 3, 22))
T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")
S = pd.Timedelta(seconds=1)
H = pd.Timedelta(hours=1)
DEFINITION = CFG.target_set("derived", "v2")
SPECS = {s.name: s for s in target_specs(CFG, DEFINITION, "xauusd")}
SIGMA = 1e-3 / np.sqrt(60)  # 10 bp over the hour; 1.29 bp over one minute
SIGMA_BPS = SIGMA / 1e-4
COSTS = CostModel.from_config(CFG, "xauusd", name="placeholder")


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


def net_usd(
    side: str,
    entry: tuple[float, float],
    exit_: tuple[float, float],
    *,
    nights: float = 0,
    multiplier: float = 1.0,
) -> float:
    """A one-lot round trip by hand with the placeholder model: fills at the ask and the bid
    moved by the slippage, 3.5 USD commission a side, financing on the entry mid."""
    slip = (0.5 + 0.1 * SIGMA_BPS) * multiplier * 1e-4
    if side == "long":
        paid_in, paid_out = entry[1] * (1 + slip), exit_[0] * (1 - slip)
        gross, rate = 100 * (paid_out - paid_in), 6.0
    else:
        paid_in, paid_out = entry[0] * (1 - slip), exit_[1] * (1 + slip)
        gross, rate = 100 * (paid_in - paid_out), 2.0
    financing = nights * 100 * sum(entry) / 2 * rate / 100 / 360
    return gross - 2 * 3.5 - financing


def test_trade_labels_net_of_the_round_trip_costs() -> None:
    # long: bought at 2000.4, sold at 2001.0: +60 USD a lot, about 32 USD of slippage and
    # commission -> a trade
    assert net_usd("long", (2000.0, 2000.4), (2001.0, 2001.4)) > 0
    assert run("tgt_trade_long_1h")["value"].iloc[0] == 1.0
    # short: sold at 2000.0, bought back at 2001.4: a loss -> no trade
    assert run("tgt_trade_short_1h")["value"].iloc[0] == 0.0
    # a smaller gain that the spread covers but the rest of the costs do not
    thin = QUOTES.assign(bid=[2000.0, 2000.6], ask=[2000.4, 2001.0])  # +20 USD after the spread
    assert net_usd("long", (2000.0, 2000.4), (2000.6, 2001.0)) < 0
    assert run("tgt_trade_long_1h", thin)["value"].iloc[0] == 0.0


def test_the_round_trip_is_the_hand_computed_one() -> None:
    windows = label_windows(
        SPECS["tgt_trade_long_1h"], QUOTES, pd.Series(SIGMA, index=pd.DatetimeIndex([T0])), CLOCK
    )
    ts = windows.ts
    bid, ask = QUOTES["bid"].to_numpy(), QUOTES["ask"].to_numpy()
    e, x = windows.entry[windows.ok], windows.exit[windows.ok]
    for side in ("long", "short"):
        pnl = round_trip_pnl(COSTS, side, ts, bid, ask, e, x, np.array([SIGMA_BPS]))
        expected = net_usd(side, (2000.0, 2000.4), (2001.0, 2001.4))
        assert pnl["net_usd"].iloc[0] == pytest.approx(expected, rel=1e-12)
        assert pnl["financing_usd"].iloc[0] == 0.0
        assert pnl["commission_usd"].iloc[0] == pytest.approx(7.0)


def test_financing_counts_three_nights_on_wednesday() -> None:
    def one_day(day: int) -> pd.DataFrame:
        t = pd.Timestamp(f"2024-03-{day} 20:00", tz="UTC")  # 16:00 New York
        quotes = pd.DataFrame(
            {
                "ts_utc": [t + 2 * S, pd.Timestamp(f"2024-03-{day + 1} 20:00:02", tz="UTC")],
                "bid": [2000.0, 2001.2],
                "ask": [2000.4, 2001.6],
            }
        )
        sigma = pd.Series(SIGMA, index=pd.DatetimeIndex([t]))
        return compute(SPECS["tgt_trade_long_1d"], quotes, sigma, CLOCK)

    # +80 USD a lot after the spread: beats one night of financing (about 33 USD), not three
    tue = net_usd("long", (2000.0, 2000.4), (2001.2, 2001.6), nights=1)
    wed = net_usd("long", (2000.0, 2000.4), (2001.2, 2001.6), nights=3)
    assert tue > 0 > wed
    assert one_day(12)["value"].iloc[0] == 1.0
    out = one_day(13)  # held over Wednesday's 17:00 New York rollover
    assert out["value"].iloc[0] == 0.0
    assert out["crosses_close"].iloc[0]


def test_a_trade_in_the_rollover_window_costs_three_times_the_slippage_in_both() -> None:
    # Entered at 16:50 New York (inside the 16:45-18:15 rollover window), 15 minutes of market
    # time: left at 18:05 New York after the daily break, inside the window again.
    t = pd.Timestamp("2024-03-12 20:50", tz="UTC")
    t_exit = pd.Timestamp("2024-03-12 22:05", tz="UTC")
    quotes = pd.DataFrame(
        {
            "ts_utc": [t + 2 * S, t_exit + 2 * S],
            "bid": [2000.0, 2004.0],
            "ask": [2000.4, 2004.4],
        }
    )
    sigma = pd.Series(SIGMA, index=pd.DatetimeIndex([t]))
    spec = SPECS["tgt_trade_long_15m"]
    windows = label_windows(spec, quotes, sigma, CLOCK)
    assert windows.ok.all()
    e, x = windows.entry, windows.exit
    bid, ask = quotes["bid"].to_numpy(), quotes["ask"].to_numpy()
    label = round_trip_pnl(COSTS, "long", windows.ts, bid, ask, e, x, np.array([SIGMA_BPS]))
    base = 0.5 + 0.1 * SIGMA_BPS
    assert label["entry_slippage_bps"].iloc[0] == pytest.approx(3 * base)
    assert label["exit_slippage_bps"].iloc[0] == pytest.approx(3 * base)
    assert label["net_usd"].iloc[0] == pytest.approx(
        net_usd("long", (2000.0, 2000.4), (2004.0, 2004.4), nights=1, multiplier=3.0)
    )
    # the backtester, the same trade: entered at t, flat again at the exit decision
    positions = pd.Series([1.0, 0.0], index=pd.DatetimeIndex([t, t_exit]))
    sigma_1m = pd.Series(SIGMA_BPS, index=positions.index)
    result = run_vectorized(
        positions, quotes, COSTS, CLOCK, capital=100_000.0, sigma_1m_bps=sigma_1m
    )
    assert result.fills["slippage_bps"].to_numpy() == pytest.approx([3 * base, 3 * base])
    (trade,) = result.trades.itertuples()
    assert trade.pnl / trade.max_lots == pytest.approx(label["net_usd"].iloc[0], rel=1e-9)
    # and the label says what the backtester's net P&L says
    value = compute(spec, quotes, sigma, CLOCK)["value"].iloc[0]
    assert value == float(trade.pnl > 0)
    # outside every window the multiplier is 1
    noon = pd.DatetimeIndex([pd.Timestamp("2024-03-12 16:00", tz="UTC")])
    assert COSTS.slippage_bps(noon, [SIGMA_BPS])[0] == pytest.approx(base)


def test_the_trade_label_is_bound_to_the_backtester_s_cost_model() -> None:
    assert all(s.costs is not None for n, s in SPECS.items() if n.startswith("tgt_trade_"))
    spec = SPECS["tgt_trade_long_1h"]
    assert spec.costs is not None
    assert spec.costs.config == CFG.cost_model_config("placeholder")
    assert spec.params["cost_model"] == "placeholder"
    # unbound specs cannot price a trade
    unbound = {s.name: s for s in expand(DEFINITION, pd.Timedelta(hours=23))}
    with pytest.raises(ConfigError, match="needs the backtester's cost model"):
        compute(unbound["tgt_trade_long_1h"], QUOTES, pd.Series(SIGMA, index=[T0]), CLOCK)
    # the label's fills must be the backtester's: latency and fill delay agree
    late = DEFINITION.model_copy(update={"params": {**DEFINITION.params, "max_fill_delay_s": 60}})
    with pytest.raises(ConfigError, match="execution parameters"):
        target_specs(CFG, late, "xauusd")
    unknown = DEFINITION.model_copy(update={"params": {**DEFINITION.params, "cost_model": "nope"}})
    with pytest.raises(ConfigError, match="not in config/costs"):
        target_specs(CFG, unknown, "xauusd")


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
