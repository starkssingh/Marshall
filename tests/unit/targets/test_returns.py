"""TGT-002: execution-aware forward returns on hand-computed cases (trading time, ADR 0026)."""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import TargetSetConfig, load_config
from xq.core.errors import ConfigError
from xq.data.calendar import MarketClock
from xq.targets.base import Lookahead, TargetSpec
from xq.targets.returns import compute, expand, lookahead, sigma_rate

T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")
S = pd.Timedelta(seconds=1)
H = pd.Timedelta(hours=1)
DAY = pd.Timedelta(hours=23)
PARAMS: dict[str, Any] = {
    "execution_latency_ms": 1000,
    "max_fill_delay_s": 300,
    "sigma_span_bars": 96,
    "vol_normalized": True,
}

# Quotes: an entry candidate just before and just after t0 + latency, and an exit candidate just
# before and just after t0 + 1h + latency.
QUOTES = pd.DataFrame(
    {
        "ts_utc": [T0 + 0.5 * S, T0 + 2 * S, T0 + H + 0.5 * S, T0 + H + 1.5 * S],
        "bid": [100.0, 101.0, 101.5, 102.0],
        "ask": [100.2, 101.2, 101.7, 102.3],
    }
)


CLOCK = MarketClock.for_range(
    load_config("research", config_dir=REPO / "config").sessions_config(),
    date(2024, 3, 10),
    date(2024, 3, 22),
)


def run(target: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series) -> pd.DataFrame:
    return compute(target, quotes, sigma, CLOCK)


def at(*times: str) -> list[pd.Timestamp]:
    return [pd.Timestamp(t, tz="UTC") for t in times]


def quotes_at(*times: str) -> pd.DataFrame:
    stamps = at(*times)
    bid = 100.0 + np.arange(len(stamps))
    return pd.DataFrame({"ts_utc": stamps, "bid": bid, "ask": bid + 0.2})


def spec(ref: str, horizon: pd.Timedelta = H, **params: Any) -> TargetSpec:
    return TargetSpec(f"t_{ref}", horizon, ref, {**PARAMS, "normalized": False, **params})  # type: ignore[arg-type]


def sigma(*times: pd.Timestamp, value: float = 0.001) -> pd.Series:
    return pd.Series(value, index=pd.DatetimeIndex(list(times)))


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("long", np.log(102.0 / 101.2)),  # buy at the entry ask, sell at the exit bid
        ("short", np.log(101.0 / 102.3)),  # sell at the entry bid, buy back at the exit ask
        ("mid", np.log((102.0 + 102.3) / (101.0 + 101.2))),
    ],
)
def test_fills_on_the_correct_side_after_the_latency(ref: str, expected: float) -> None:
    out = run(spec(ref), QUOTES, sigma(T0))
    assert out["value"].iloc[0] == pytest.approx(expected)
    assert out["label_start"].iloc[0] == T0 + 2 * S  # not the 0.5 s quote: before t0 + latency
    assert out["label_end"].iloc[0] == T0 + H + 1.5 * S
    assert np.isnan(out["scale"].iloc[0])


def test_fill_delay_is_the_later_fill_after_its_intended_time() -> None:
    out = run(spec("long"), QUOTES, sigma(T0, T0 + pd.Timedelta(minutes=15)))
    # entry intended at t0 + 1 s, filled at t0 + 2 s; exit intended at t0 + 1h + 1 s, filled 0.5 s
    # later: the label's delay is the larger one
    assert out["fill_delay_s"].iloc[0] == pytest.approx(1.0)
    assert np.isnan(out["fill_delay_s"].iloc[1])  # no label, no delay


def test_a_decision_before_the_close_holds_over_the_daily_break() -> None:
    # Tuesday 16:30 EDT: 30 market minutes to the close, the other 30 after the 18:00 reopen.
    quotes = quotes_at("2024-03-12 20:30:02", "2024-03-12 22:30:03")
    out = run(spec("long"), quotes, sigma(*at("2024-03-12 20:30")))
    assert out["label_start"].iloc[0] == pd.Timestamp("2024-03-12 20:30:02", tz="UTC")
    assert out["label_end"].iloc[0] == pd.Timestamp("2024-03-12 22:30:03", tz="UTC")
    assert out["value"].iloc[0] == pytest.approx(np.log(101.0 / 100.2))
    assert out["crosses_close"].iloc[0]
    assert out["fill_delay_s"].iloc[0] == pytest.approx(2.0)


@pytest.mark.parametrize(
    "decision",
    [
        "2024-03-12 21:00",  # 17:00 EDT, the close itself (the last bar of the day is available)
        "2024-03-12 21:30",  # inside the daily break
        "2024-03-16 12:00",  # Saturday
        "2024-03-17 21:59:59",  # one second before the Sunday reopen
    ],
)
def test_a_decision_while_the_market_is_closed_gets_no_label(decision: str) -> None:
    # ADR 0032: no entry at the reopen; the row keeps no value, fills or delay.
    quotes = quotes_at("2024-03-17 22:00:05", "2024-03-17 23:00:02", "2024-03-12 22:00:05")
    quotes = quotes.sort_values("ts_utc", ignore_index=True)
    out = run(spec("mid"), quotes, sigma(*at(decision)))
    assert np.isnan(out["value"].iloc[0])
    assert pd.isna(out["label_start"].iloc[0])
    assert pd.isna(out["label_end"].iloc[0])
    assert not out["crosses_close"].iloc[0]
    assert np.isnan(out["fill_delay_s"].iloc[0])


def test_a_decision_at_the_reopen_is_labelled() -> None:
    # 18:00 EDT exactly is open again: the same quotes give a label from that decision.
    quotes = quotes_at("2024-03-12 22:00:05", "2024-03-12 23:00:02")
    out = run(spec("mid"), quotes, sigma(*at("2024-03-12 22:00")))
    assert out["label_start"].iloc[0] == pd.Timestamp("2024-03-12 22:00:05", tz="UTC")
    assert out["label_end"].iloc[0] == pd.Timestamp("2024-03-12 23:00:02", tz="UTC")
    assert not out["crosses_close"].iloc[0]


def test_a_friday_decision_is_labelled_over_the_weekend() -> None:
    # Friday 16:00 EDT, 4 hours: 1 hour to the close, 3 hours after the Sunday 18:00 EDT open.
    quotes = quotes_at("2024-03-15 20:00:01", "2024-03-18 01:00:01")
    out = run(spec("long", pd.Timedelta(hours=4)), quotes, sigma(*at("2024-03-15 20:00")))
    assert out["label_end"].iloc[0] == pd.Timestamp("2024-03-18 01:00:01", tz="UTC")
    assert np.isfinite(out["value"].iloc[0])
    assert out["crosses_close"].iloc[0]
    # A wall-clock exit (Saturday 00:00:01 UTC) would have had no quote and no label.
    wall_exit = quotes_at("2024-03-15 20:00:01", "2024-03-16 00:00:01")
    assert np.isnan(
        run(spec("long", pd.Timedelta(hours=4)), wall_exit, sigma(*at("2024-03-15 20:00")))[
            "value"
        ].iloc[0]
    )


def test_no_label_when_the_fill_comes_too_late_or_never() -> None:
    later = T0 + pd.Timedelta(minutes=15)  # next quote after later + 1 s is 45 minutes away
    out = run(spec("long"), QUOTES, sigma(T0, later))
    assert np.isfinite(out["value"].iloc[0])
    assert np.isnan(out["value"].iloc[1])
    assert pd.isna(out["label_start"].iloc[1])
    assert pd.isna(out["label_end"].iloc[1])
    beyond = run(spec("long", pd.Timedelta(hours=4)), QUOTES, sigma(T0))
    assert np.isnan(beyond["value"].iloc[0])  # no quote after the 4-hour exit


def test_zero_latency_fills_at_quotes_exactly_at_the_intended_times() -> None:
    at = T0 + 0.5 * S  # a quote at exactly t, and one at exactly t + 1h
    out = run(spec("long", execution_latency_ms=0), QUOTES, sigma(at))
    assert out["label_start"].iloc[0] == at
    assert out["label_end"].iloc[0] == at + H
    assert out["value"].iloc[0] == pytest.approx(np.log(101.5 / 100.2))
    assert out["fill_delay_s"].iloc[0] == 0.0


def test_vol_normalized_variant_divides_by_sigma_at_t_scaled_to_the_horizon() -> None:
    raw = run(spec("long"), QUOTES, sigma(T0))
    normalized = run(spec("long", normalized=True), QUOTES, sigma(T0, value=0.001))
    scale = 0.001 * np.sqrt(60)
    assert normalized["scale"].iloc[0] == pytest.approx(scale)
    assert normalized["value"].iloc[0] == pytest.approx(raw["value"].iloc[0] / scale)
    zero = run(spec("long", normalized=True), QUOTES, sigma(T0, value=0.0))
    assert np.isnan(zero["value"].iloc[0])


def test_empty_quotes_give_no_labels() -> None:
    empty = QUOTES.iloc[0:0]
    out = run(spec("mid"), empty, sigma(T0))
    assert np.isnan(out["value"].iloc[0])


def test_configured_set_expands_to_named_targets() -> None:
    definition = load_config("research", config_dir=REPO / "config").target_set("fwd_returns", "v1")
    names = [s.name for s in expand(definition, DAY)]
    assert len(names) == 4 * 3 * 2
    assert "fwd_ret_long_1h" in names
    assert "fwd_ret_short_1d_vol" in names
    assert lookahead(definition, DAY) == Lookahead(
        market=pd.Timedelta(hours=23) + S, wall=pd.Timedelta(seconds=300)
    )
    horizons = {s.name: s.horizon for s in expand(definition, DAY)}
    assert horizons["fwd_ret_long_1d"] == pd.Timedelta(hours=23)  # one trading day (ADR 0032)
    assert horizons["fwd_ret_long_4h"] == pd.Timedelta(hours=4)
    plain = TargetSetConfig(
        kind="forward_return",
        horizons=["1h"],
        price_refs=["mid"],
        params={**PARAMS, "vol_normalized": False},
    )
    assert [s.name for s in expand(plain, DAY)] == ["fwd_ret_mid_1h"]


@pytest.mark.parametrize(
    "params",
    [
        {k: v for k, v in PARAMS.items() if k != "execution_latency_ms"},
        {**PARAMS, "max_fill_delay_s": 0},
        {**PARAMS, "unexpected": 1},
    ],
)
def test_params_are_validated(params: dict[str, Any]) -> None:
    definition = TargetSetConfig(
        kind="forward_return", horizons=["1h"], price_refs=["long"], params=params
    )
    with pytest.raises(ConfigError, match="forward_return params"):
        expand(definition, DAY)


def test_sigma_rate_is_per_square_root_minute() -> None:
    definition = TargetSetConfig(
        kind="forward_return", horizons=["1h"], price_refs=["long"], params=PARAMS
    )
    index = pd.date_range(T0, periods=5, freq="15min")
    close = pd.Series([100.0, 101.0, 100.0, 101.0, 102.0], index=index)
    rate = sigma_rate(close, definition, pd.Timedelta(minutes=15))
    per_bar = sigma_rate(close, definition, pd.Timedelta(minutes=1))
    np.testing.assert_allclose(rate.to_numpy(), per_bar.to_numpy() / np.sqrt(15))
    assert np.isnan(rate.iloc[0])  # no return yet


def test_one_day_is_one_trading_day_of_23_market_hours() -> None:
    # Tuesday 10:00 EDT: 7 market hours to the 17:00 close, the 17:00-18:00 break is skipped, and
    # 16 more hours end Wednesday 10:00 EDT, one trading day later on the session clock.
    t = pd.Timestamp("2024-03-12 14:00", tz="UTC")
    exit_time = pd.Timestamp("2024-03-13 14:00:01", tz="UTC")
    quotes = pd.DataFrame(
        {
            "ts_utc": [t + S, exit_time],
            "bid": [100.0, 101.0],
            "ask": [100.2, 101.2],
        }
    )
    one_day = TargetSpec("x", DAY, "mid", {**PARAMS, "normalized": False})
    out = compute(one_day, quotes, pd.Series([1.0], index=pd.DatetimeIndex([t])), CLOCK)
    assert out["label_end"].iloc[0] == exit_time
    assert out["crosses_close"].iloc[0]
    assert out["value"].iloc[0] == pytest.approx(np.log(101.1 / 100.1))
    # Four hours are four market hours: Tuesday 15:00 EDT + 4 h ends at 20:00 EDT, after the break.
    four = TargetSpec("x", 4 * H, "mid", {**PARAMS, "normalized": False})
    late = pd.Timestamp("2024-03-12 19:00", tz="UTC")
    quotes = pd.DataFrame(
        {
            "ts_utc": [late + S, pd.Timestamp("2024-03-13 00:00:01", tz="UTC")],
            "bid": [100.0, 101.0],
            "ask": [100.2, 101.2],
        }
    )
    out = compute(four, quotes, pd.Series([1.0], index=pd.DatetimeIndex([late])), CLOCK)
    assert out["label_end"].iloc[0] == pd.Timestamp("2024-03-13 00:00:01", tz="UTC")


def test_vol_scale_uses_1380_minutes_for_one_day() -> None:
    t = pd.Timestamp("2024-03-12 14:00", tz="UTC")
    quotes = pd.DataFrame(
        {
            "ts_utc": [t + S, pd.Timestamp("2024-03-13 14:00:01", tz="UTC")],
            "bid": [100.0, 101.0],
            "ask": [100.2, 101.2],
        }
    )
    spec_vol = TargetSpec("x_vol", DAY, "mid", {**PARAMS, "normalized": True})
    out = compute(spec_vol, quotes, pd.Series([0.001], index=pd.DatetimeIndex([t])), CLOCK)
    assert out["scale"].iloc[0] == pytest.approx(0.001 * np.sqrt(1380))
