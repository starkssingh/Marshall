"""DATA-008: bar construction on hand-computed cases."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from xq.core.config import BarsConfig, load_config
from xq.core.types import Timeframe
from xq.data.adapters.base import TICK_SCHEMA
from xq.data.bars import BAR_SCHEMA, build_bars, build_version, exclude_mask, find_gaps
from xq.data.flags import TickFlag

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
MINUTE = 60 * 10**9


def ns(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").value)


def ticks(rows: list[tuple[str, float, float, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": [ns(t) for t, *_ in rows],
            "bid": [b for _, b, _, _ in rows],
            "ask": [a for _, _, a, _ in rows],
            "bid_size": np.nan,
            "ask_size": np.nan,
            "flags": [f for *_, f in rows],
            "raw_file_id": "r",
            "row_num": range(len(rows)),
        }
    ).astype(TICK_SCHEMA)


@pytest.fixture(scope="module")
def cfg() -> BarsConfig:
    return load_config("research", config_dir=REPO_CONFIG).bars_config()


SAMPLE = ticks(
    [
        ("2024-03-11 12:00:10", 2170.00, 2170.30, 0),
        ("2024-03-11 12:00:20", 2170.50, 2170.70, int(TickFlag.STALE)),  # flagged, included
        ("2024-03-11 12:00:30", 2169.90, 2170.40, 0),
        ("2024-03-11 12:00:40", 2180.00, 2170.00, int(TickFlag.CROSSED)),  # excluded
        ("2024-03-11 12:00:50", 2170.10, 2170.35, 0),
        ("2024-03-11 12:02:05", 2171.00, 2171.20, 0),  # 12:01 has no ticks
    ]
)


def test_one_minute_bar_by_hand(cfg: BarsConfig) -> None:
    bars = build_bars(
        SAMPLE,
        Timeframe.M1,
        exclude_flags=exclude_mask(cfg),
        latency_ns=250_000_000,
        coverage_end_ns=ns("2024-03-11 12:02:05"),
    )
    assert list(bars.columns) == list(BAR_SCHEMA)
    assert bars["bar_start_utc"].tolist() == [ns("2024-03-11 12:00"), ns("2024-03-11 12:02")]
    first = bars.iloc[0]
    assert first["available_at_utc"] == ns("2024-03-11 12:01:00.250")
    assert [first[f"bid_{p}"] for p in ("open", "high", "low", "close")] == [
        2170.00,
        2170.50,
        2169.90,
        2170.10,
    ]
    assert [first[f"ask_{p}"] for p in ("open", "high", "low", "close")] == [
        2170.30,
        2170.70,
        2170.30,
        2170.35,
    ]
    assert [first[f"mid_{p}"] for p in ("open", "high", "low", "close")] == pytest.approx(
        [2170.15, 2170.60, 2170.15, 2170.225]
    )
    assert first["tick_count"] == 4
    assert first["spread_mean"] == pytest.approx(0.3125)
    assert first["spread_med"] == pytest.approx(0.275)
    assert first["spread_max"] == pytest.approx(0.50)
    assert first["spread_close"] == pytest.approx(0.25)
    assert first["n_flagged"] == 1
    assert first["n_excluded"] == 1
    assert first["trading_day"] == date(2024, 3, 11)
    assert bool(first["is_complete"])
    assert not bool(bars.iloc[1]["is_complete"])  # the data ends inside this bar


@pytest.mark.parametrize(
    ("tf", "expected_starts"),
    [
        # Friday before the US DST change closes at 22:00 UTC (17:00 EST); Monday after it starts
        # at 21:00 UTC (Sunday 17:00 EDT).
        (Timeframe.D1, ["2024-03-07 22:00", "2024-03-10 21:00"]),
        # 4h bars start every 4 hours from the 17:00 New York trading-day start.
        (Timeframe.H4, ["2024-03-08 18:00", "2024-03-10 21:00", "2024-03-11 17:00"]),
    ],
)
def test_trading_day_anchored_bars_across_dst(tf: Timeframe, expected_starts: list[str]) -> None:
    frame = ticks(
        [
            ("2024-03-08 21:59:59", 2170.0, 2170.3, 0),  # Fri 16:59:59 EST
            ("2024-03-10 22:00:00", 2171.0, 2171.3, 0),  # Sun 18:00 EDT
            ("2024-03-11 20:59:00", 2172.0, 2172.3, 0),  # Mon 16:59 EDT
        ]
    )
    bars = build_bars(frame, tf, exclude_flags=0, latency_ns=0, coverage_end_ns=ns("2024-03-12"))
    assert bars["bar_start_utc"].tolist() == [ns(s) for s in expected_starts]
    if tf is Timeframe.D1:
        assert bars["available_at_utc"].tolist() == [ns("2024-03-08 22:00"), ns("2024-03-11 21:00")]
        assert bars["trading_day"].tolist() == [date(2024, 3, 8), date(2024, 3, 11)]


def test_daily_bar_after_the_november_change_starts_at_22_utc() -> None:
    frame = ticks([("2024-11-04 12:00", 2700.0, 2700.3, 0)])
    bars = build_bars(frame, Timeframe.D1, exclude_flags=0, latency_ns=0, coverage_end_ns=0)
    assert bars["bar_start_utc"].tolist() == [ns("2024-11-03 22:00")]
    assert bars["available_at_utc"].tolist() == [ns("2024-11-04 22:00")]


def test_only_excluded_ticks_give_no_bar(cfg: BarsConfig) -> None:
    frame = ticks([("2024-03-11 12:00:10", 2170.5, 2170.0, int(TickFlag.CROSSED))])
    bars = build_bars(
        frame, Timeframe.M1, exclude_flags=exclude_mask(cfg), latency_ns=0, coverage_end_ns=0
    )
    assert bars.empty
    assert list(bars.columns) == list(BAR_SCHEMA)


def test_unsorted_ticks_are_rejected() -> None:
    frame = ticks([("2024-03-11 12:00:10", 1.0, 1.1, 0), ("2024-03-11 12:00:05", 1.0, 1.1, 0)])
    with pytest.raises(ValueError, match="sorted"):
        build_bars(frame, Timeframe.M1, exclude_flags=0, latency_ns=0, coverage_end_ns=0)


def test_gaps_and_expected_open() -> None:
    bars = pd.DataFrame(
        {
            "bar_start_utc": [
                ns("2024-03-08 20:58"),
                ns("2024-03-08 20:59"),
                ns("2024-03-10 22:00"),
                ns("2024-03-10 22:05"),
            ]
        }
    )
    market_open = np.array([ns("2024-03-07 23:00"), ns("2024-03-10 22:00")])
    market_close = np.array([ns("2024-03-08 21:00"), ns("2024-03-11 21:00")])
    gaps = find_gaps(bars, Timeframe.M1, market_open, market_close)
    assert gaps["gap_start_utc"].tolist() == [ns("2024-03-08 21:00"), ns("2024-03-10 22:01")]
    assert gaps["gap_end_utc"].tolist() == [ns("2024-03-10 22:00"), ns("2024-03-10 22:05")]
    assert gaps["expected_open"].tolist() == [False, True]  # the weekend is closed


def test_spike_cannot_exclude_ticks_from_bars(cfg: BarsConfig) -> None:
    with pytest.raises(ValidationError, match="confirmed by later ticks"):
        BarsConfig.model_validate({**cfg.model_dump(), "exclude_flags": ["SPIKE"]})


def test_build_version_tracks_settings_and_clean_rules(cfg: BarsConfig) -> None:
    version = build_version(cfg, "c1-aaaa")
    assert version.startswith(f"{cfg.version}-")
    assert build_version(cfg, "c1-bbbb") != version
    changed = BarsConfig.model_validate({**cfg.model_dump(), "publication_latency_ms": 5})
    assert build_version(changed, "c1-aaaa") != version
