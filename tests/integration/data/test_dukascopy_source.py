"""DATA-013 acceptance: Dukascopy files ingest, clean, bar and validate under their UTC clock.

The fixtures are UTC, so the files' hours never move; the market's daily and weekly gaps move by
an hour in UTC when New York changes DST, and they must land where the calendar puts them. A
misdeclared clock (the MT5 broker clock `NY+7`) must be caught by the same checks.
"""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from helpers.dukascopy_fixtures import (
    BI5_DIR,
    CSV_DIR,
    CSV_NAME,
    SYMBOL,
    bi5_fixture_quotes,
    bi5_relative_path,
    csv_fixture_text,
    hour_records,
)
from helpers.pipeline import REPO, config, run_pipeline
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.core.time import trading_days
from xq.data.adapters import DukascopyTickAdapter, build_adapter
from xq.data.adapters.dukascopy import decode_bi5, parse_bi5_name
from xq.data.flags import TickFlag
from xq.data.raw_store import ingest
from xq.data.sessions import build_session_table
from xq.quality.registry import Status
from xq.quality.validate import validate_source
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import DataSource, RawFile

MIN_GAP = pd.Timedelta(minutes=30)
TOLERANCE = pd.Timedelta(minutes=20)
WEEK = [date(2024, 3, d) for d in (11, 12, 13, 14, 15)]
DST_FLAGS = TickFlag.TS_DST_AMBIGUOUS | TickFlag.TS_DST_NONEXISTENT

# (market closes shortly before, reopens shortly after), in UTC. The same instants as the MT5
# fixtures of these weeks: one market, two clocks.
EXPECTED_GAPS = {
    "bi5": [
        ("2024-03-06 22:00", "2024-03-06 23:00"),  # Wed rollover, EST
        ("2024-03-07 22:00", "2024-03-07 23:00"),  # Thu rollover, EST
        ("2024-03-08 22:00", "2024-03-10 22:00"),  # Fri 17:00 EST close -> Sun 18:00 EDT open
        ("2024-03-11 21:00", "2024-03-11 22:00"),  # Mon rollover, EDT
        ("2024-03-12 21:00", "2024-03-12 22:00"),  # Tue rollover, EDT
    ],
    "csv": [
        ("2024-10-30 21:00", "2024-10-30 22:00"),  # Wed rollover, EDT
        ("2024-10-31 21:00", "2024-10-31 22:00"),  # Thu rollover, EDT
        ("2024-11-01 21:00", "2024-11-03 23:00"),  # Fri 17:00 EDT close -> Sun 18:00 EST open
        ("2024-11-04 22:00", "2024-11-04 23:00"),  # Mon rollover, EST
        ("2024-11-05 22:00", "2024-11-05 23:00"),  # Tue rollover, EST
    ],
}
FIXTURE_DIRS = {"bi5": BI5_DIR, "csv": CSV_DIR}


def canonical_ticks(fixture: str, clock: str = "UTC") -> pd.DataFrame:
    cfg = config(Path("."), **{"sources.dukascopy.clock": clock})
    adapter = build_adapter(cfg, "dukascopy")
    assert isinstance(adapter, DukascopyTickAdapter)
    frames = [
        adapter.to_canonical(adapter.read(ref)) for ref in adapter.discover(FIXTURE_DIRS[fixture])
    ]
    ticks = pd.concat(frames, ignore_index=True)
    return ticks.sort_values("ts_utc", kind="stable").reset_index(drop=True)


def times_of(ticks: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(ticks["ts_utc"].to_numpy(), unit="ns", utc=True))


def observed_gaps(times: pd.DatetimeIndex) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    deltas = times[1:] - times[:-1]
    return [(times[i], times[i + 1]) for i in np.flatnonzero(deltas > MIN_GAP)]


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


def test_committed_bi5_fixtures_match_the_generator() -> None:
    expected = hour_records(bi5_fixture_quotes())
    files = sorted(BI5_DIR.rglob("*.bi5"))
    assert [p.relative_to(BI5_DIR) for p in files] == [
        bi5_relative_path(SYMBOL, hour) for hour in expected
    ]
    # Compare decoded records: LZMA bytes may differ between liblzma versions.
    for path, rows in zip(files, expected.values(), strict=True):
        np.testing.assert_array_equal(decode_bi5(path.read_bytes()), rows)


def test_committed_csv_fixture_matches_the_generator() -> None:
    assert (CSV_DIR / CSV_NAME).read_text(encoding="utf-8") == csv_fixture_text()


def test_no_hourly_file_covers_the_weekend_gap() -> None:
    hours = sorted(parse_bi5_name(p.name)[1] for p in BI5_DIR.rglob("*.bi5"))
    friday_close, sunday_open = (
        pd.Timestamp("2024-03-08 22:00", tz="UTC"),
        pd.Timestamp("2024-03-10 22:00", tz="UTC"),
    )
    assert not [h for h in hours if friday_close <= h < sunday_open]
    last_before = max(h for h in hours if h < friday_close)
    first_after = min(h for h in hours if h >= sunday_open)
    assert (last_before, first_after) == (friday_close - pd.Timedelta(hours=1), sunday_open)


@pytest.mark.parametrize("fixture", ["bi5", "csv"])
def test_gaps_land_at_expected_utc_hours_across_dst(fixture: str) -> None:
    ticks = canonical_ticks(fixture)
    assert not (ticks["flags"].to_numpy() & DST_FLAGS).any()  # UTC has no DST
    gaps = observed_gaps(times_of(ticks))
    assert matches(gaps, EXPECTED_GAPS[fixture]), [(str(a), str(b)) for a, b in gaps]


@pytest.mark.parametrize("fixture", ["bi5", "csv"])
def test_every_tick_falls_inside_calendar_market_hours(fixture: str) -> None:
    times = times_of(canonical_ticks(fixture))
    cfg = config(Path("."))
    table = build_session_table(
        cfg.sessions_config(), date(2024, 1, 1), date(2024, 12, 31)
    ).set_index("trading_day")
    days = [d.item() for d in trading_days(times)]
    opens = pd.DatetimeIndex(table.loc[days, "market_open_utc"])
    closes = pd.DatetimeIndex(table.loc[days, "market_close_utc"])
    inside = (times >= opens) & (times < closes)
    assert inside.all(), times[~inside][:5]


@pytest.mark.parametrize("fixture", ["bi5", "csv"])
def test_misdeclared_broker_clock_is_detected(fixture: str) -> None:
    # Reading UTC files as MT5 server time (NY+7) moves every tick 2-3 hours earlier.
    gaps = observed_gaps(times_of(canonical_ticks(fixture, clock="NY+7")))
    assert not matches(gaps, EXPECTED_GAPS[fixture])
    for (last, _), (close, _) in zip(gaps, EXPECTED_GAPS[fixture], strict=True):
        assert pd.Timestamp(close, tz="UTC") - last > pd.Timedelta(hours=1)


def _ingest(cfg: AppConfig, path: Path, run_id: str) -> tuple[int, int, int]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    try:
        result = ingest(cfg, "dukascopy", path, engine=engine, run_id=run_id, git_sha="test")
    finally:
        engine.dispose()
    return len(result.ingested), len(result.skipped), result.rows


def test_ingest_records_every_hourly_file_and_reingest_is_a_no_op(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    hours = hour_records(bi5_fixture_quotes())
    rows = sum(len(r) for r in hours.values())
    assert _ingest(cfg, BI5_DIR, "01RUN000000000000000000001") == (len(hours), 0, rows)
    assert _ingest(cfg, BI5_DIR, "01RUN000000000000000000002") == (0, len(hours), 0)
    ingested_csv = _ingest(cfg, CSV_DIR, "01RUN000000000000000000003")
    assert ingested_csv[:2] == (1, 0)

    engine = create_db_engine(cfg.database_url())
    with session_factory(engine)() as session:
        source = session.get(DataSource, "dukascopy")
        records = list(session.scalars(select(RawFile).where(RawFile.source_id == "dukascopy")))
    engine.dispose()
    assert source is not None
    assert (source.clock_convention, source.feed_type, source.price_type) == (
        "UTC",
        "vendor_ticks",
        "bid_ask_ticks",
    )
    by_name = {r.original_name: r for r in records}
    first = by_name["XAUUSD_2024-03-06_00h_ticks.bi5"]
    assert first.path.startswith("raw/dukascopy/xauusd/2024/03/")
    assert first.first_ts_utc is not None
    assert pd.Timestamp(first.first_ts_utc).floor("h") == pd.Timestamp("2024-03-06 00:00", tz="UTC")
    assert by_name[CSV_NAME].row_count == ingested_csv[2]


def test_clean_week_of_hourly_files_passes_every_quality_check(
    tmp_path: Path, dukascopy_clean_week_dir: Path
) -> None:
    cfg = config(tmp_path)
    engine = run_pipeline(cfg, dukascopy_clean_week_dir, source="dukascopy")
    result = validate_source(
        cfg, engine, "dukascopy", run_id="01QRUN0000000000000000DK01", git_sha="test"
    )
    engine.dispose()
    assert result.days == WEEK
    failing = {(r.check_id, r.trading_day) for r in result.results if r.status is not Status.PASS}
    assert not failing


def test_misdeclared_clock_fails_calendar_checks(
    tmp_path: Path, dukascopy_clean_week_dir: Path
) -> None:
    cfg = config(tmp_path, **{"sources.dukascopy.clock": "NY+7"})
    engine = run_pipeline(cfg, dukascopy_clean_week_dir, spreads=False, source="dukascopy")
    result = validate_source(
        cfg, engine, "dukascopy", run_id="01QRUN0000000000000000DK02", git_sha="test"
    )
    engine.dispose()
    by_check = {(r.check_id, r.trading_day): r for r in result.results}
    gap_failures = {
        day
        for (check, day), r in by_check.items()
        if check == "cal.gap_location" and r.status is Status.FAIL
    }
    closed_failures = {
        day
        for (check, day), r in by_check.items()
        if check == "cal.closed_market_ticks" and r.status is Status.FAIL
    }
    assert gap_failures == closed_failures == set(WEEK)


def test_cli_ingests_hourly_files(tmp_path: Path) -> None:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--set",
        f"paths.root={tmp_path}",
        "--set",
        f"paths.migrations_dir={REPO / 'migrations'}",
        "--set",
        "logging.console=false",
    ]
    runner = CliRunner()
    result = runner.invoke(
        app, [*common, "ingest", "--source", "dukascopy", "--path", str(BI5_DIR)]
    )
    assert result.exit_code == 0, result.output
    assert f"ingested {len(hour_records(bi5_fixture_quotes()))} file(s)" in result.stdout
