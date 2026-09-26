"""DATA-003: SourceAdapter protocol and the MT5 tick-export adapter."""

import codecs
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from xq.core.config import AppConfig, SourceConfig, load_config
from xq.core.errors import ConfigError, SourceFormatError
from xq.data.adapters import (
    TICK_SCHEMA,
    Mt5TickAdapter,
    RawFileRef,
    SourceAdapter,
    build_adapter,
    discover_files,
    validate_tick_frame,
)
from xq.data.flags import TickFlag

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
HEADER = "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>"
SAMPLE = "\n".join(
    [
        HEADER,
        "2024.03.11\t10:00:00.100\t2170.10\t\t\t\t2",  # only the bid is known yet
        "2024.03.11\t10:00:00.200\t\t2170.40\t\t\t4",
        "2024.03.11\t10:00:00.300\t2170.20\t\t\t\t2",
        "2024.03.11\t10:00:00.250\t2170.25\t2170.45\t\t\t6",  # out of order in the file
        "",
    ]
)


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_config("research", config_dir=REPO_CONFIG)


@pytest.fixture
def adapter(cfg: AppConfig) -> Mt5TickAdapter:
    built = build_adapter(cfg, "mt5_primary")
    assert isinstance(built, Mt5TickAdapter)
    return built


def write(tmp_path: Path, name: str, content: str | bytes) -> RawFileRef:
    path = tmp_path / name
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return RawFileRef(path=path, original_name=name, size=path.stat().st_size)


def test_adapter_satisfies_the_protocol_and_declares_conventions(adapter: Mt5TickAdapter) -> None:
    protocol_view: SourceAdapter = adapter
    assert protocol_view.source_id == "mt5_primary"
    assert str(adapter.clock) == "NY+7"
    assert adapter.price_type == "bid_ask_ticks"


def test_read_keeps_every_row_in_file_order_unmodified(
    adapter: Mt5TickAdapter, tmp_path: Path
) -> None:
    raw = adapter.read(write(tmp_path, "t.csv", SAMPLE))
    assert raw["row_num"].tolist() == [0, 1, 2, 3]
    assert raw["ts_raw"].tolist() == [
        "2024.03.11 10:00:00.100",
        "2024.03.11 10:00:00.200",
        "2024.03.11 10:00:00.300",
        "2024.03.11 10:00:00.250",
    ]
    assert raw["bid"].tolist()[0] == 2170.10
    assert np.isnan(raw["ask"].iloc[0])  # empty field stays empty, not filled
    assert raw["mt5_flags"].tolist() == [2, 4, 2, 6]
    assert raw["last"].isna().all()


def test_to_canonical_carries_quotes_forward_and_sorts(
    adapter: Mt5TickAdapter, tmp_path: Path
) -> None:
    raw = adapter.read(write(tmp_path, "t.csv", SAMPLE))
    raw["raw_file_id"] = "rf_test"
    ticks = validate_tick_frame(adapter.to_canonical(raw))

    assert list(ticks.columns) == list(TICK_SCHEMA)
    assert ticks["row_num"].tolist() == [0, 1, 3, 2]  # stable sort by UTC time
    assert ticks["bid"].tolist() == [2170.10, 2170.10, 2170.25, 2170.20]
    assert ticks["ask"].iloc[1:].tolist() == [2170.40, 2170.45, 2170.40]
    flags = ticks["flags"].to_numpy()
    assert flags[0] & TickFlag.MISSING_QUOTE  # ask not yet seen
    assert flags[2] & TickFlag.TS_OUT_OF_ORDER
    assert flags[1] == 0
    assert flags[3] == 0
    assert (ticks["raw_file_id"] == "rf_test").all()
    # Server 10:00 on 11 March 2024 (UTC+3 after the US DST change) is 07:00 UTC.
    first = pd.Timestamp(int(ticks["ts_utc"].iloc[0]), unit="ns", tz="UTC")
    assert first == pd.Timestamp("2024-03-11 07:00:00.100", tz="UTC")


def test_to_canonical_without_provenance_column(adapter: Mt5TickAdapter, tmp_path: Path) -> None:
    ticks = adapter.to_canonical(adapter.read(write(tmp_path, "t.csv", SAMPLE)))
    assert ticks["raw_file_id"].isna().all()
    validate_tick_frame(ticks)


@pytest.mark.parametrize(
    ("encoding", "bom", "line_end"),
    [
        ("utf-16-le", codecs.BOM_UTF16_LE, "\r\n"),
        ("utf-8", codecs.BOM_UTF8, "\r\n"),
        ("utf-8", b"", "\n"),
    ],
)
def test_encodings_and_line_endings(
    adapter: Mt5TickAdapter, tmp_path: Path, encoding: str, bom: bytes, line_end: str
) -> None:
    content = bom + SAMPLE.replace("\n", line_end).encode(encoding)
    raw = adapter.read(write(tmp_path, "t.csv", content))
    assert len(raw) == 4
    assert raw["ts_raw"].iloc[0] == "2024.03.11 10:00:00.100"


def test_times_without_milliseconds(adapter: Mt5TickAdapter, tmp_path: Path) -> None:
    content = f"{HEADER}\n2024.03.11\t10:00:01\t2170.10\t2170.30\t\t\t6\n"
    ticks = adapter.to_canonical(adapter.read(write(tmp_path, "t.csv", content)))
    assert pd.Timestamp(int(ticks["ts_utc"].iloc[0]), unit="ns", tz="UTC") == pd.Timestamp(
        "2024-03-11 07:00:01", tz="UTC"
    )


def test_file_mixing_times_with_and_without_milliseconds(
    adapter: Mt5TickAdapter, tmp_path: Path
) -> None:
    content = "\n".join(
        [
            HEADER,
            "2024.03.11\t10:00:01\t2170.10\t2170.30\t\t\t6",
            "2024.03.11\t10:00:01.250\t2170.15\t\t\t\t2",
            "2024.03.11\t10:00:02\t\t2170.40\t\t\t4",
            "",
        ]
    )
    ticks = adapter.to_canonical(adapter.read(write(tmp_path, "t.csv", content)))
    stamps = [pd.Timestamp(int(ns), unit="ns", tz="UTC") for ns in ticks["ts_utc"]]
    assert stamps == [
        pd.Timestamp("2024-03-11 07:00:01", tz="UTC"),
        pd.Timestamp("2024-03-11 07:00:01.250", tz="UTC"),
        pd.Timestamp("2024-03-11 07:00:02", tz="UTC"),
    ]


def test_header_only_file_gives_empty_frames(adapter: Mt5TickAdapter, tmp_path: Path) -> None:
    raw = adapter.read(write(tmp_path, "t.csv", HEADER + "\n"))
    assert raw.empty
    ticks = validate_tick_frame(adapter.to_canonical(raw))
    assert ticks.empty


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("<DATE>\t<TIME>\t<BID>\n2024.03.11\t10:00:00\t1\n", "missing \\['<ASK>'\\]"),
        (f"{HEADER}\n2024.03.11\t10:00:00.1\t21x0.1\t2170.3\t\t\t6\n", "<BID> value '21x0.1'"),
        (f"{HEADER}\n2024-03-11\t10:00:00.1\t2170.1\t2170.3\t\t\t6\n", "unparseable timestamp"),
    ],
)
def test_malformed_files_are_rejected(
    adapter: Mt5TickAdapter, tmp_path: Path, content: str, message: str
) -> None:
    ref = write(tmp_path, "t.csv", content)
    with pytest.raises(SourceFormatError, match=message):
        adapter.to_canonical(adapter.read(ref))


def test_undecodable_file_is_rejected(adapter: Mt5TickAdapter, tmp_path: Path) -> None:
    with pytest.raises(SourceFormatError, match="cannot decode"):
        adapter.read(write(tmp_path, "t.csv", b"<DATE>\t<TIME>\xc3\x28"))  # invalid UTF-8


def test_discover_filters_sorts_and_skips_hidden(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / ".hidden").mkdir()
    for name in ["b/2.csv", "a.csv", "b/1.txt", "notes.md", ".hidden/x.csv", ".dot.csv"]:
        (tmp_path / name).write_text("x")
    refs = discover_files(tmp_path, ["*.csv", "*.txt"])
    assert [r.path.relative_to(tmp_path).as_posix() for r in refs] == [
        "a.csv",
        "b/1.txt",
        "b/2.csv",
    ]
    assert refs[0].original_name == "a.csv"
    assert refs[0].size == 1
    assert [r.original_name for r in discover_files(tmp_path / "a.csv", ["*.csv"])] == ["a.csv"]
    with pytest.raises(SourceFormatError, match="no such file"):
        discover_files(tmp_path / "missing", ["*.csv"])


def test_source_config_validation(cfg: AppConfig) -> None:
    source = cfg.source("mt5_primary").model_dump()
    assert SourceConfig.model_validate({**source, "clock": "ny+7"}).clock == "NY+7"
    with pytest.raises(ValueError, match="unknown clock convention"):
        SourceConfig.model_validate({**source, "clock": "server time"})
    with pytest.raises(ConfigError, match="unknown source 'nope'"):
        cfg.source("nope")
    with pytest.raises(ConfigError, match="unknown instrument 'eurusd'"):
        load_config(
            "research", {"sources.mt5_primary.instrument": "eurusd"}, config_dir=REPO_CONFIG
        )
