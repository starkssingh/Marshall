"""DATA-013: the Dukascopy tick adapter (native hourly .bi5 files and dukascopy-node CSV)."""

import lzma
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.dukascopy_fixtures import encode_bi5
from xq.core.config import AppConfig, SourceConfig, load_config
from xq.core.errors import NaiveTimestampError, SourceFormatError
from xq.data.adapters import (
    TICK_SCHEMA,
    DukascopyTickAdapter,
    RawFileRef,
    SourceAdapter,
    build_adapter,
    validate_tick_frame,
)
from xq.data.adapters.dukascopy import (
    BI5_RECORD,
    bi5_file_name,
    datafeed_url,
    decode_bi5,
    parse_bi5_name,
)
from xq.data.flags import TickFlag

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
BASE = "https://datafeed.dukascopy.com/datafeed"
DST_FLAGS = TickFlag.TS_DST_AMBIGUOUS | TickFlag.TS_DST_NONEXISTENT


def utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def records(*rows: tuple[int, int, int, float, float]) -> np.ndarray:
    return np.array(list(rows), dtype=BI5_RECORD)


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_config("research", config_dir=REPO_CONFIG)


@pytest.fixture
def adapter(cfg: AppConfig) -> DukascopyTickAdapter:
    built = build_adapter(cfg, "dukascopy")
    assert isinstance(built, DukascopyTickAdapter)
    return built


def write(tmp_path: Path, name: str, content: str | bytes) -> RawFileRef:
    path = tmp_path / name
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return RawFileRef(path=path, original_name=name, size=path.stat().st_size)


def ts_utc(ticks: pd.DataFrame) -> list[pd.Timestamp]:
    return [pd.Timestamp(int(v), unit="ns", tz="UTC") for v in ticks["ts_utc"]]


def test_adapter_satisfies_the_protocol_and_declares_conventions(
    adapter: DukascopyTickAdapter,
) -> None:
    protocol_view: SourceAdapter = adapter
    assert protocol_view.source_id == "dukascopy"
    assert str(adapter.clock) == "UTC"
    assert adapter.price_type == "bid_ask_ticks"
    assert (adapter.vendor_symbol, adapter.point_scale) == ("XAUUSD", 1000)


def test_file_names_carry_the_utc_hour() -> None:
    hour = utc("2024-03-10 07:00")
    name = bi5_file_name("XAUUSD", hour)
    assert name == "XAUUSD_2024-03-10_07h_ticks.bi5"
    assert parse_bi5_name(name) == ("XAUUSD", hour)
    assert bi5_file_name("XAUUSD", pd.Timestamp("2024-03-10 08:00", tz="Europe/Zurich")) == name


@pytest.mark.parametrize(
    "name",
    [
        "13h_ticks.bi5",  # the vendor's own name: no date, no symbol
        "XAUUSD_2024-02-30_13h_ticks.bi5",
        "XAUUSD_2024-03-11_24h_ticks.bi5",
        "xauusd_2024-03-11_13h_ticks.bi5",
        "XAUUSD_2024-03-11_13h_ticks.csv",
    ],
)
def test_bad_file_names_are_refused(name: str) -> None:
    with pytest.raises(SourceFormatError):
        parse_bi5_name(name)


def test_hours_must_be_aware_and_whole() -> None:
    with pytest.raises(NaiveTimestampError):
        bi5_file_name("XAUUSD", pd.Timestamp("2024-03-11 13:00"))
    with pytest.raises(ValueError, match="not the start of a UTC hour"):
        datafeed_url(BASE, "XAUUSD", utc("2024-03-11 13:30"))


@pytest.mark.parametrize(
    ("hour", "path"),
    [
        ("2024-03-11 13:00", "XAUUSD/2024/02/11/13h_ticks.bi5"),  # March is month 02
        ("2024-01-01 00:00", "XAUUSD/2024/00/01/00h_ticks.bi5"),
        ("2003-12-31 23:00", "XAUUSD/2003/11/31/23h_ticks.bi5"),
    ],
)
def test_datafeed_urls_number_months_from_zero(hour: str, path: str) -> None:
    assert datafeed_url(BASE + "/", "XAUUSD", utc(hour)) == f"{BASE}/{path}"


def test_decode_round_trips_and_rejects_corrupt_payloads() -> None:
    rows = records((0, 2034155, 2033985, 1.25, 0.5), (3_599_999, 2034160, 2034000, 0.0, 2.0))
    decoded = decode_bi5(encode_bi5(rows))
    assert decoded.dtype == BI5_RECORD
    np.testing.assert_array_equal(decoded, rows)
    assert len(decode_bi5(b"")) == 0
    with pytest.raises(SourceFormatError, match="not an LZMA stream"):
        decode_bi5(b"not lzma at all")
    partial = lzma.compress(rows.tobytes()[:-3], format=lzma.FORMAT_ALONE)
    with pytest.raises(SourceFormatError, match="not whole 20-byte records"):
        decode_bi5(partial)


def test_read_bi5_keeps_every_record_and_scales_points(
    adapter: DukascopyTickAdapter, tmp_path: Path
) -> None:
    rows = records(
        (100, 2034155, 2033985, 1.25, 0.5),
        (2500, 2034160, 2034000, 0.75, 1.0),
        (1500, 2034170, 2034010, 0.25, 0.125),  # out of order in the file
        (3_599_999, 2034180, 2034020, 1.0, 1.0),
    )
    raw = adapter.read(write(tmp_path, "XAUUSD_2024-03-11_13h_ticks.bi5", encode_bi5(rows)))
    assert raw["row_num"].tolist() == [0, 1, 2, 3]
    assert raw["ts_raw"].tolist() == ["100", "2500", "1500", "3599999"]
    assert raw["ask_points"].tolist() == [2034155, 2034160, 2034170, 2034180]
    assert raw["ask"].tolist() == [2034.155, 2034.16, 2034.17, 2034.18]
    assert raw["bid"].tolist() == [2033.985, 2034.0, 2034.01, 2034.02]
    assert raw["ask_volume"].tolist() == [1.25, 0.75, 0.25, 1.0]
    assert raw["bid_volume"].tolist() == [0.5, 1.0, 0.125, 1.0]

    raw["raw_file_id"] = "rf_test"
    ticks = validate_tick_frame(adapter.to_canonical(raw))
    assert list(ticks.columns) == list(TICK_SCHEMA)
    assert ticks["row_num"].tolist() == [0, 2, 1, 3]  # stable sort by UTC time
    hour = utc("2024-03-11 13:00")
    assert ts_utc(ticks) == [hour + pd.Timedelta(milliseconds=ms) for ms in (100, 1500, 2500)] + [
        hour + pd.Timedelta(milliseconds=3_599_999)
    ]
    flags = ticks.set_index("row_num")["flags"]
    assert flags[2] & TickFlag.TS_OUT_OF_ORDER
    assert flags[[0, 1, 3]].eq(0).all()
    assert ticks["bid_size"].isna().all()  # vendor volume units are not documented
    assert ticks["ask_size"].isna().all()
    assert (ticks["raw_file_id"] == "rf_test").all()


def test_an_empty_hour_has_no_rows(adapter: DukascopyTickAdapter, tmp_path: Path) -> None:
    raw = adapter.read(write(tmp_path, "XAUUSD_2024-03-09_12h_ticks.bi5", b""))
    assert raw.empty
    validate_tick_frame(adapter.to_canonical(raw))


@pytest.mark.parametrize(
    ("name", "rows", "message"),
    [
        (
            "XAUUSD_2024-03-11_13h_ticks.bi5",
            records((3_600_000, 2034155, 2033985, 1.0, 1.0)),
            "outside",
        ),
        ("XAUUSD_2024-03-11_13h_ticks.bi5", records((-1, 2034155, 2033985, 1.0, 1.0)), "outside"),
        ("EURUSD_2024-03-11_13h_ticks.bi5", records((0, 108155, 108150, 1.0, 1.0)), "not the"),
        ("13h_ticks.bi5", records((0, 2034155, 2033985, 1.0, 1.0)), "hourly file name"),
    ],
)
def test_bi5_files_that_cannot_be_trusted_are_refused(
    adapter: DukascopyTickAdapter, tmp_path: Path, name: str, rows: np.ndarray, message: str
) -> None:
    with pytest.raises(SourceFormatError, match=message):
        adapter.read(write(tmp_path, name, encode_bi5(rows)))


@pytest.mark.parametrize(
    "hours",
    [
        # US DST starts at 07:00 UTC on 10 March 2024; EU DST at 01:00 UTC on 31 March.
        ["2024-03-10 06:00", "2024-03-10 07:00", "2024-03-31 01:00"],
        # US DST ends at 06:00 UTC on 3 November 2024 (01:00-02:00 New York happens twice).
        ["2024-11-03 05:00", "2024-11-03 06:00", "2024-10-27 01:00"],
    ],
)
def test_utc_hours_are_unaffected_by_dst(
    adapter: DukascopyTickAdapter, tmp_path: Path, hours: list[str]
) -> None:
    for hour in map(utc, hours):
        rows = records((0, 2150000, 2149800, 1.0, 1.0), (1_800_000, 2150100, 2149900, 1.0, 1.0))
        name = bi5_file_name("XAUUSD", hour)
        ticks = adapter.to_canonical(adapter.read(write(tmp_path, name, encode_bi5(rows))))
        assert ts_utc(ticks) == [hour, hour + pd.Timedelta(minutes=30)]
        assert not (ticks["flags"].to_numpy() & DST_FLAGS).any()


CSV = "\n".join(
    [
        "timestamp,askPrice,bidPrice,askVolume,bidVolume",
        "1710162000100,2170.4,2170.1,0.0012,0.0009",
        "1710162000300,2170.45,2170.25,0.001,0.002",
        "1710162000200,2170.44,2170.2,0.001,0.001",  # out of order in the file
    ]
)


def test_read_csv_parses_unix_milliseconds(adapter: DukascopyTickAdapter, tmp_path: Path) -> None:
    raw = adapter.read(write(tmp_path, "XAUUSD_2024-03.csv", CSV))
    assert raw["row_num"].tolist() == [0, 1, 2]
    assert raw["ts_raw"].tolist() == ["1710162000100", "1710162000300", "1710162000200"]
    assert raw["bid"].tolist() == [2170.1, 2170.25, 2170.2]
    assert raw["ask"].tolist() == [2170.4, 2170.45, 2170.44]
    assert raw["ask_volume"].tolist() == [0.0012, 0.001, 0.001]
    ticks = validate_tick_frame(adapter.to_canonical(raw))
    assert ticks["row_num"].tolist() == [0, 2, 1]
    assert ts_utc(ticks)[0] == utc("2024-03-11 13:00:00.100")
    assert ticks.set_index("row_num")["flags"][2] & TickFlag.TS_OUT_OF_ORDER


@pytest.mark.parametrize(
    "stamp",
    [
        "2024-03-11T13:00:00.100Z",
        "2024-03-11T13:00:00.100+00:00",
        "2024-03-11 13:00:00.100",  # naive text is read in the declared clock (UTC)
    ],
)
def test_read_csv_parses_utc_iso_text(
    adapter: DukascopyTickAdapter, tmp_path: Path, stamp: str
) -> None:
    text = f"timestamp,askPrice,bidPrice\n{stamp},2170.4,2170.1\n"
    ticks = adapter.to_canonical(adapter.read(write(tmp_path, "t.csv", text)))
    assert ts_utc(ticks) == [utc("2024-03-11 13:00:00.100")]
    assert ticks["bid_size"].isna().all()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("timestamp,askPrice,bidPrice\n2024-03-11T15:00:00.100+02:00,1,1\n", "not UTC"),
        ("timestamp,askPrice,bidPrice\nyesterday,1,1\n", "unparseable timestamp"),
        ("timestamp,askPrice,bidPrice\n1710162000100,abc,1\n", "askPrice value"),
        ("time,ask,bid\n1710162000100,1,1\n", "not a dukascopy-node tick CSV"),
    ],
)
def test_bad_csv_files_are_refused(
    adapter: DukascopyTickAdapter, tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(SourceFormatError, match=message):
        adapter.read(write(tmp_path, "t.csv", text))


def test_a_missing_csv_side_is_flagged_not_filled(
    adapter: DukascopyTickAdapter, tmp_path: Path
) -> None:
    text = "timestamp,askPrice,bidPrice\n1710162000100,2170.4,\n1710162000200,2170.5,2170.2\n"
    ticks = adapter.to_canonical(adapter.read(write(tmp_path, "t.csv", text)))
    assert np.isnan(ticks["bid"].iloc[0])  # no carry-forward, nothing invented
    assert ticks["flags"].iloc[0] & TickFlag.MISSING_QUOTE
    assert ticks["flags"].iloc[1] == 0


def test_empty_csv_has_no_rows(adapter: DukascopyTickAdapter, tmp_path: Path) -> None:
    for content in ("", "timestamp,askPrice,bidPrice,askVolume,bidVolume\n"):
        raw = adapter.read(write(tmp_path, "empty.csv", content))
        assert raw.empty
        validate_tick_frame(adapter.to_canonical(raw))


def test_other_file_types_are_refused(adapter: DukascopyTickAdapter, tmp_path: Path) -> None:
    with pytest.raises(SourceFormatError, match=r"expected a \.bi5 or \.csv file"):
        adapter.read(write(tmp_path, "manifest.jsonl", "{}"))


def test_discover_ignores_the_download_manifest_and_partial_files(
    adapter: DukascopyTickAdapter, tmp_path: Path
) -> None:
    folder = tmp_path / "XAUUSD" / "2024" / "03" / "11"
    folder.mkdir(parents=True)
    (folder / "XAUUSD_2024-03-11_13h_ticks.bi5").write_bytes(b"")
    (folder / ".XAUUSD_2024-03-11_14h_ticks.bi5.part").write_bytes(b"")
    (tmp_path / "XAUUSD" / "manifest.jsonl").write_text("")
    assert [r.original_name for r in adapter.discover(tmp_path)] == [
        "XAUUSD_2024-03-11_13h_ticks.bi5"
    ]


def test_the_adapter_requires_its_vendor_encoding(cfg: AppConfig) -> None:
    declared = cfg.source("dukascopy").model_dump()
    for field in ("vendor_symbol", "point_scale"):
        with pytest.raises(ValidationError, match=field):
            SourceConfig.model_validate({**declared, field: None})
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({**declared, "point_scale": 0})
