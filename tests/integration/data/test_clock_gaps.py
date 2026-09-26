"""DATA-006 acceptance: gaps land at the expected UTC hours on both sides of each US DST change.

The fixture files are in MT5 server time (NY+7). If the declared clock is right, the daily 17:00
New York rollover gap, the Friday close and the Sunday open appear at the UTC hours below — which
move by an hour when New York changes DST. Declaring the look-alike `tz:Europe/Athens` ("EET")
instead puts them an hour off in the weeks when US and EU DST disagree.
"""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.mt5_fixtures import FIXTURE_DIR, FIXTURES, fixture_text
from xq.core.config import AppConfig, load_config
from xq.core.time import trading_days
from xq.data.adapters import Mt5TickAdapter, RawFileRef, build_adapter
from xq.data.sessions import build_session_table

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
MARCH = "XAUUSD_mt5_ticks_2024-03-06_2024-03-13.csv"
NOVEMBER = "XAUUSD_mt5_ticks_2024-10-30_2024-11-06.csv"
MIN_GAP = pd.Timedelta(minutes=30)
TOLERANCE = pd.Timedelta(minutes=20)

# (last tick before the gap is shortly before, first tick after is shortly after), in UTC.
EXPECTED_GAPS = {
    MARCH: [
        ("2024-03-06 22:00", "2024-03-06 23:00"),  # Wed rollover, EST
        ("2024-03-07 22:00", "2024-03-07 23:00"),  # Thu rollover, EST
        ("2024-03-08 22:00", "2024-03-10 22:00"),  # Fri 17:00 EST close -> Sun 18:00 EDT open
        ("2024-03-11 21:00", "2024-03-11 22:00"),  # Mon rollover, EDT
        ("2024-03-12 21:00", "2024-03-12 22:00"),  # Tue rollover, EDT
    ],
    NOVEMBER: [
        ("2024-10-30 21:00", "2024-10-30 22:00"),  # Wed rollover, EDT
        ("2024-10-31 21:00", "2024-10-31 22:00"),  # Thu rollover, EDT
        ("2024-11-01 21:00", "2024-11-03 23:00"),  # Fri 17:00 EDT close -> Sun 18:00 EST open
        ("2024-11-04 22:00", "2024-11-04 23:00"),  # Mon rollover, EST
        ("2024-11-05 22:00", "2024-11-05 23:00"),  # Tue rollover, EST
    ],
}


def fixture_ref(name: str) -> RawFileRef:
    path = FIXTURE_DIR / name
    return RawFileRef(path=path, original_name=name, size=path.stat().st_size)


def config(clock: str = "NY+7") -> AppConfig:
    return load_config("research", {"sources.mt5_primary.clock": clock}, config_dir=REPO_CONFIG)


def canonical_times(name: str, clock: str = "NY+7") -> pd.DatetimeIndex:
    adapter = build_adapter(config(clock), "mt5_primary")
    assert isinstance(adapter, Mt5TickAdapter)
    ticks = adapter.to_canonical(adapter.read(fixture_ref(name)))
    return pd.DatetimeIndex(pd.to_datetime(ticks["ts_utc"].to_numpy(), unit="ns", utc=True))


def observed_gaps(times: pd.DatetimeIndex) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    deltas = times[1:] - times[:-1]
    starts = np.flatnonzero(deltas > MIN_GAP)
    return [(times[i], times[i + 1]) for i in starts]


def matches(gaps: list[tuple[pd.Timestamp, pd.Timestamp]], expected: list[tuple[str, str]]) -> bool:
    if len(gaps) != len(expected):
        return False
    for (last, first), (close, reopen) in zip(gaps, expected, strict=True):
        close_ts, reopen_ts = pd.Timestamp(close, tz="UTC"), pd.Timestamp(reopen, tz="UTC")
        if not (
            close_ts - TOLERANCE <= last < close_ts <= reopen_ts <= first < reopen_ts + TOLERANCE
        ):
            return False
    return True


@pytest.mark.parametrize("name", [MARCH, NOVEMBER])
def test_committed_fixtures_match_the_generator(name: str) -> None:
    assert (FIXTURE_DIR / name).read_text(encoding="utf-8") == fixture_text(name)
    assert name in FIXTURES


@pytest.mark.parametrize("name", [MARCH, NOVEMBER])
def test_gaps_land_at_expected_utc_hours_across_dst(name: str) -> None:
    gaps = observed_gaps(canonical_times(name))
    assert matches(gaps, EXPECTED_GAPS[name]), [(str(a), str(b)) for a, b in gaps]


@pytest.mark.parametrize("name", [MARCH, NOVEMBER])
def test_every_tick_falls_inside_calendar_market_hours(name: str) -> None:
    times = canonical_times(name)
    table = build_session_table(
        config().sessions_config(), date(2024, 1, 1), date(2024, 12, 31)
    ).set_index("trading_day")
    days = [d.item() for d in trading_days(times)]
    opens = pd.DatetimeIndex(table.loc[days, "market_open_utc"])
    closes = pd.DatetimeIndex(table.loc[days, "market_close_utc"])
    inside = (times >= opens) & (times < closes)
    assert inside.all(), times[~inside][:5]


@pytest.mark.parametrize(
    ("name", "wrong_days"),
    [
        # US on DST from 10 March, EU only from 31 March: Mon/Tue rollovers come out an hour late.
        (MARCH, ["2024-03-11", "2024-03-12"]),
        # EU off DST from 27 October, US only from 3 November: Wed/Thu/Fri come out an hour late.
        (NOVEMBER, ["2024-10-30", "2024-10-31", "2024-11-01"]),
    ],
)
def test_misdeclared_eet_clock_is_detected(name: str, wrong_days: list[str]) -> None:
    gaps = observed_gaps(canonical_times(name, clock="tz:Europe/Athens"))
    assert not matches(gaps, EXPECTED_GAPS[name])
    late = {
        str(last.date())
        for (last, _), (close, _) in zip(gaps, EXPECTED_GAPS[name], strict=True)
        if last >= pd.Timestamp(close, tz="UTC")
    }
    assert late == set(wrong_days)
