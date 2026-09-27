"""SIGNAL-003: the signal filters — the PLACEHOLDER regime filter lets everything through and says
so, sessions and blackouts follow the local-time calendar across DST (and agree with the dataset
calendar columns), the volatility band is inclusive, the spread filter is strict against a
causal hour-of-week median."""

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import SESSIONS, ns
from xq.datasets.calendar_columns import calendar_columns
from xq.signals.filters import (
    REGIME_PLACEHOLDER,
    BlackoutFilter,
    CalendarLookup,
    FilterContext,
    HourOfWeekSpreads,
    PassThroughRegimeFilter,
    RegimeFilter,
    SessionFilter,
    SpreadFilter,
    VolatilityBandFilter,
)
from xq.signals.schema import RegimeState

CALENDAR = CalendarLookup(SESSIONS)


def at(text: str, *, spread: float | None = 0.2, sigma: float | None = 0.01) -> FilterContext:
    return FilterContext(ns(text), spread, sigma)


def test_the_regime_filter_is_a_marked_pass_through_until_a_regime_model_exists() -> None:
    regime_filter: RegimeFilter = PassThroughRegimeFilter(allowed=["trend"])
    assert regime_filter.allowed == ("trend",)
    assert regime_filter.name == "regime"
    assert "PLACEHOLDER" in regime_filter.pass_label
    assert regime_filter.pass_label == REGIME_PLACEHOLDER
    ranging = RegimeState(
        ts=pd.Timestamp("2024-03-12 14:00", tz="UTC"),
        model_version="none",
        probs={"range": 1.0},
        label="range",
        age_bars=3,
    )
    context = FilterContext(ns("2024-03-12 14:00"), 0.2, 0.01, ranging)
    assert regime_filter.check(context) is None  # accepts a RegimeState, blocks nothing
    assert regime_filter.check(at("2024-03-12 14:00")) is None


def test_sessions_follow_local_time_across_the_uk_dst_change() -> None:
    london = SessionFilter(CALENDAR, ["london"])
    # before 31 March 2024 London is on GMT: 08:00-17:00 UTC
    assert london.check(at("2024-03-12 07:59")) == "outside the allowed sessions ['london']"
    assert london.check(at("2024-03-12 08:00")) is None
    assert london.check(at("2024-03-12 16:59:59")) is None
    assert london.check(at("2024-03-12 17:00")) is not None
    # on BST from 31 March: 07:00-16:00 UTC
    assert london.check(at("2024-04-02 07:30")) is None
    assert london.check(at("2024-04-02 16:30")) is not None
    either = SessionFilter(CALENDAR, ["tokyo", "london"])
    assert either.check(at("2024-03-12 02:00")) is None  # 11:00 in Tokyo
    with pytest.raises(ValueError, match="configured sessions"):
        SessionFilter(CALENDAR, ["lunch"])
    with pytest.raises(ValueError, match="configured sessions"):
        SessionFilter(CALENDAR, [])


def test_blackouts_cover_the_rollover_and_data_release_windows() -> None:
    blackout = BlackoutFilter(CALENDAR, ["rollover", "us_data_release"])
    # 16:45-18:15 New York (EDT from 10 March): 20:45-22:15 UTC
    assert blackout.check(at("2024-03-12 20:44")) is None
    assert blackout.check(at("2024-03-12 20:45")) == "inside the rollover window"
    # 08:30 New York release, 5 minutes before to 30 after: 12:25-13:00 UTC
    assert blackout.check(at("2024-03-12 12:24")) is None
    assert blackout.check(at("2024-03-12 12:25")) == "inside the us_data_release window"
    assert blackout.check(at("2024-03-12 12:59")) is not None
    assert blackout.check(at("2024-03-12 13:00")) is None
    with pytest.raises(ValueError, match="event windows"):
        BlackoutFilter(CALENDAR, ["lunch"])


def test_the_minute_lookup_agrees_with_the_dataset_calendar_columns() -> None:
    rng = np.random.default_rng(0)
    t = np.sort(rng.integers(ns("2024-03-01"), ns("2024-04-10"), 3_000))
    columns = calendar_columns(pd.DatetimeIndex(pd.to_datetime(t, unit="ns", utc=True)), SESSIONS)
    for name in ("tokyo", "london", "new_york", "london_new_york"):
        looked_up = [CALENDAR.in_session(int(x), name) for x in t]
        np.testing.assert_array_equal(looked_up, columns[f"in_{name}"].to_numpy(bool))
    for name in ("rollover", "us_data_release"):
        looked_up = [CALENDAR.in_window(int(x), name) for x in t]
        np.testing.assert_array_equal(looked_up, columns[f"in_{name}_window"].to_numpy(bool))


def test_the_volatility_band_is_inclusive_and_needs_a_sigma_hat() -> None:
    band = VolatilityBandFilter(0.005, 0.02)
    assert band.check(at("2024-03-12 14:00", sigma=0.005)) is None
    assert band.check(at("2024-03-12 14:00", sigma=0.02)) is None
    assert band.check(at("2024-03-12 14:00", sigma=0.0049)) is not None
    assert "outside" in str(band.check(at("2024-03-12 14:00", sigma=0.03)))
    assert band.check(at("2024-03-12 14:00", sigma=None)) == "no sigma-hat at the decision"
    with pytest.raises(ValueError, match="band"):
        VolatilityBandFilter(0.02, 0.01)


def test_the_hour_of_week_median_is_causal_with_a_fallback() -> None:
    spreads = HourOfWeekSpreads(min_obs=3)
    # Tuesday 10:xx New York (14:xx UTC in EDT)
    assert HourOfWeekSpreads.bucket(ns("2024-03-12 14:30")) == 1 * 24 + 10
    assert spreads.reference(ns("2024-03-12 14:30")) is None
    for day, spread in ((5, 0.2), (6, 0.4), (7, 0.3)):  # other hours: only the overall median
        spreads.observe(ns(f"2024-03-0{day} 03:00"), spread)
    assert spreads.reference(ns("2024-03-12 14:30")) == pytest.approx(0.3)
    for day, spread in ((12, 0.1), (19, 0.1), (26, 0.12)):
        spreads.observe(ns(f"2024-03-{day} 14:10"), spread)
    # the Tuesday-10:00 bucket now has three spreads of its own: its median
    assert spreads.reference(ns("2024-04-02 14:30")) == pytest.approx(0.1)


def test_the_spread_filter_is_strict() -> None:
    spreads = HourOfWeekSpreads(min_obs=1)
    spreads.observe(ns("2024-03-05 14:00"), 0.1)
    spread = SpreadFilter(3.0, spreads)
    assert spread.check(at("2024-03-12 14:00", spread=0.29)) is None
    reason = spread.check(at("2024-03-12 14:00", spread=0.3 + 1e-12))
    assert str(reason).startswith("spread 0.3 not below 3 x the median 0.1")
    assert spread.check(at("2024-03-12 14:00", spread=None)) == "no quote at the decision"
    empty = SpreadFilter(3.0, HourOfWeekSpreads(min_obs=5))
    assert empty.check(at("2024-03-12 14:00")) == "no hour-of-week spread reference yet"
