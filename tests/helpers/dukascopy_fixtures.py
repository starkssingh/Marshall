"""Deterministic synthetic Dukascopy tick files: native hourly ``.bi5`` and dukascopy-node CSV.

The market model is the one of `helpers.mt5_fixtures` — quotes only while the synthetic broker is
open (closed Friday 17:00 to Sunday 18:00 New York and 17:00-18:00 New York every day) — but
timestamps are UTC, as Dukascopy writes them, and every row carries both sides with prices on the
0.001 grid of Dukascopy's XAUUSD points. They are **not market data**.

- ``bi5/``: one LZMA file per UTC hour that has ticks, in the downloader's layout
  ``XAUUSD/<yyyy>/<mm>/<dd>/XAUUSD_<yyyy-mm-dd>_<HH>h_ticks.bi5``, for the week of the US DST
  start (Sunday 10 March 2024) with its weekend gap;
- ``csv/``: one dukascopy-node tick CSV (Unix-ms timestamps) for the week of the US DST end
  (Sunday 3 November 2024) with its weekend gap.

Regenerate the committed fixtures (the suite checks them against this generator: decoded records
for ``.bi5``, since LZMA bytes may differ between liblzma versions, and bytes for the CSV)::

    PYTHONPATH=tests uv run python -m helpers.dukascopy_fixtures
"""

from __future__ import annotations

import lzma
from pathlib import Path

import numpy as np
import pandas as pd

from helpers.mt5_fixtures import broker_market_open
from xq.core.seeds import make_rng
from xq.data.adapters.dukascopy import BI5_RECORD, bi5_file_name

SYMBOL = "XAUUSD"
POINT_SCALE = 1000
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "dukascopy"
BI5_DIR = FIXTURE_ROOT / "bi5"
CSV_DIR = FIXTURE_ROOT / "csv"
#: The ``.bi5`` week: spans the US DST start (Sunday 10 March 2024) and its weekend gap.
BI5_WEEK = ("2024-03-06", "2024-03-13", 30306, 2150.0)
CSV_NAME = "XAUUSD_ticks_2024-10-30_2024-11-06.csv"
#: The CSV week: spans the US DST end (Sunday 3 November 2024) and its weekend gap.
CSV_WEEK = ("2024-10-30", "2024-11-06", 31030, 2780.0)
CSV_HEADER = "timestamp,askPrice,bidPrice,askVolume,bidVolume"


def synthetic_quotes(
    start_utc: str,
    end_utc: str,
    *,
    seed: int,
    start_price: float = 2150.0,
    mean_interval_s: float = 90.0,
) -> pd.DataFrame:
    """Quotes in ``[start_utc, end_utc)`` while the synthetic broker is open.

    Columns: ``ts_utc`` (tz-aware, whole milliseconds), ``ask_points``, ``bid_points`` (int64,
    1000 per USD), ``ask_volume``, ``bid_volume`` (float32 values, as in a ``.bi5`` record).
    Every row changes the quote.
    """
    rng = make_rng(seed)
    start = pd.Timestamp(start_utc, tz="UTC")
    end = pd.Timestamp(end_utc, tz="UTC")
    span_ms = int((end - start) / pd.Timedelta(milliseconds=1))
    count = int(span_ms / (mean_interval_s * 1000) * 1.2) + 10
    offsets = np.cumsum(np.maximum(1, rng.exponential(mean_interval_s * 1000, count).astype(int)))
    offsets = offsets[offsets < span_ms]
    times = pd.DatetimeIndex(start + pd.to_timedelta(offsets, unit="ms"))
    times = times[broker_market_open(times)]

    mid = start_price * POINT_SCALE + np.cumsum(rng.normal(0.0, 80.0, len(times)))
    spread = 200 + 10 * rng.integers(0, 21, len(times))
    bid = np.round(mid - spread / 2).astype(np.int64)
    ask = bid + spread.astype(np.int64)
    ask[1:][(bid[1:] == bid[:-1]) & (ask[1:] == ask[:-1])] += 1
    volumes = rng.integers(1, 100, (2, len(times))).astype(np.float32) / np.float32(10_000)
    return pd.DataFrame(
        {
            "ts_utc": times,
            "ask_points": ask,
            "bid_points": bid,
            "ask_volume": volumes[0],
            "bid_volume": volumes[1],
        }
    )


def hour_records(quotes: pd.DataFrame) -> dict[pd.Timestamp, np.ndarray]:
    """`BI5_RECORD` arrays keyed by UTC hour start, for the hours that have quotes."""
    hours = quotes["ts_utc"].dt.floor("h")
    result = {}
    for hour, rows in quotes.groupby(hours, sort=True):
        records = np.zeros(len(rows), dtype=BI5_RECORD)
        records["ms"] = (rows["ts_utc"] - hour) // pd.Timedelta(milliseconds=1)
        records["ask"] = rows["ask_points"]
        records["bid"] = rows["bid_points"]
        records["ask_volume"] = rows["ask_volume"]
        records["bid_volume"] = rows["bid_volume"]
        result[pd.Timestamp(hour)] = records
    return result


def encode_bi5(records: np.ndarray) -> bytes:
    """A ``.bi5`` payload: the records, big-endian, LZMA-compressed in the "alone" format."""
    if len(records) == 0:
        return b""
    return lzma.compress(records.astype(BI5_RECORD).tobytes(), format=lzma.FORMAT_ALONE)


def bi5_relative_path(symbol: str, hour: pd.Timestamp) -> Path:
    """Where the downloader puts the file of `hour` below its output directory."""
    return Path(symbol, f"{hour:%Y}", f"{hour:%m}", f"{hour:%d}", bi5_file_name(symbol, hour))


def write_bi5_hours(quotes: pd.DataFrame, directory: Path, symbol: str = SYMBOL) -> list[Path]:
    """Write one ``.bi5`` file per UTC hour with quotes; return the paths written."""
    written = []
    for hour, records in hour_records(quotes).items():
        path = directory / bi5_relative_path(symbol, hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_bi5(records))
        written.append(path)
    return written


def canonical_to_quotes(ticks: pd.DataFrame) -> pd.DataFrame:
    """Quotes from canonical ticks (``ts_utc`` int64 ns, ``bid``, ``ask``), e.g. `dense_ticks`."""
    return pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(ticks["ts_utc"].to_numpy(), unit="ns", utc=True).floor("ms"),
            "ask_points": np.round(ticks["ask"].to_numpy() * POINT_SCALE).astype(np.int64),
            "bid_points": np.round(ticks["bid"].to_numpy() * POINT_SCALE).astype(np.int64),
            "ask_volume": np.float32(0.0001),
            "bid_volume": np.float32(0.0001),
        }
    )


def to_csv_text(quotes: pd.DataFrame) -> str:
    """Render quotes as dukascopy-node writes a tick CSV: Unix-ms timestamps, shortest numbers."""
    millis = quotes["ts_utc"].to_numpy(dtype="datetime64[ms]").view(np.int64)
    lines = [CSV_HEADER]
    for when, ask, bid, ask_volume, bid_volume in zip(
        millis,
        quotes["ask_points"],
        quotes["bid_points"],
        quotes["ask_volume"],
        quotes["bid_volume"],
        strict=True,
    ):
        prices = (repr(ask / POINT_SCALE), repr(bid / POINT_SCALE))
        volumes = (repr(float(ask_volume)), repr(float(bid_volume)))
        lines.append(",".join((str(when), *prices, *volumes)))
    return "\n".join(lines)


def bi5_fixture_quotes() -> pd.DataFrame:
    """The quotes behind the committed ``.bi5`` fixtures."""
    start, end, seed, price = BI5_WEEK
    return synthetic_quotes(start, end, seed=seed, start_price=price)


def csv_fixture_text() -> str:
    """The exact content of the committed CSV fixture."""
    start, end, seed, price = CSV_WEEK
    return to_csv_text(synthetic_quotes(start, end, seed=seed, start_price=price))


def main() -> None:
    for old in BI5_DIR.rglob("*.bi5"):
        old.unlink()
    written = write_bi5_hours(bi5_fixture_quotes(), BI5_DIR)
    print(f"wrote {len(written)} hourly files under {BI5_DIR}")
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    (CSV_DIR / CSV_NAME).write_text(csv_fixture_text(), encoding="utf-8")
    print(f"wrote {CSV_DIR / CSV_NAME}")


if __name__ == "__main__":
    main()
