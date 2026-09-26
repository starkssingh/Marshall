"""Deterministic synthetic MT5 tick exports.

The generator mimics a retail broker feed quoted in MT5 server time (``NY+7``: UTC+2 in US winter,
UTC+3 in US summer). Ticks exist only while the market is open by the broker's rules, expressed
directly in New York wall-clock time — independently of `xq.data.calendar`, so tests can use it
to check the calendar and the timestamp normalization:

- closed Friday 17:00 to Sunday 18:00 New York (weekend) and 17:00-18:00 every day (daily break).

Like real MT5 exports, each row carries only the side(s) that changed (``<FLAGS>`` 2 = bid,
4 = ask, 6 = both).

Regenerate the committed fixtures with::

    PYTHONPATH=tests uv run python -m helpers.mt5_fixtures
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from xq.core.seeds import make_rng

NEW_YORK = "America/New_York"
SERVER_SHIFT = pd.Timedelta(hours=7)
HEADER = "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>"
BID, ASK, BOTH = 2, 4, 6

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ticks"
FIXTURES = {
    # Spans the US DST start (Sunday 10 March 2024).
    "XAUUSD_mt5_ticks_2024-03-06_2024-03-13.csv": ("2024-03-06", "2024-03-13", 20240306, 2150.0),
    # Spans the US DST end (Sunday 3 November 2024), after the EU change on 27 October.
    "XAUUSD_mt5_ticks_2024-10-30_2024-11-06.csv": ("2024-10-30", "2024-11-06", 20241030, 2780.0),
}


def broker_market_open(ts_utc: pd.DatetimeIndex) -> np.ndarray:
    """True where the synthetic broker quotes (New York wall-clock rules)."""
    ny = ts_utc.tz_convert(NEW_YORK)
    minutes = np.asarray(ny.hour * 60 + ny.minute)
    weekday = np.asarray(ny.weekday)
    daily_break = (minutes >= 17 * 60) & (minutes < 18 * 60)
    weekend = (
        (weekday == 5)
        | ((weekday == 6) & (minutes < 18 * 60))
        | ((weekday == 4) & (minutes >= 17 * 60))
    )
    return ~(daily_break | weekend)


def synthetic_ticks(
    start_utc: str,
    end_utc: str,
    *,
    seed: int,
    start_price: float = 2150.0,
    mean_interval_s: float = 90.0,
) -> pd.DataFrame:
    """Ticks in ``[start_utc, end_utc)`` with columns ``ts_utc``, ``bid``, ``ask``, ``changed``."""
    rng = make_rng(seed)
    start = pd.Timestamp(start_utc, tz="UTC")
    end = pd.Timestamp(end_utc, tz="UTC")
    span_ms = int((end - start) / pd.Timedelta(milliseconds=1))
    count = int(span_ms / (mean_interval_s * 1000) * 1.2) + 10
    offsets = np.cumsum(np.maximum(1, rng.exponential(mean_interval_s * 1000, count).astype(int)))
    offsets = offsets[offsets < span_ms]
    times = pd.DatetimeIndex(start + pd.to_timedelta(offsets, unit="ms"))
    times = times[broker_market_open(times)]

    bids, asks, changed = [], [], []
    mid = start_price
    bid = ask = np.nan
    for i in range(len(times)):
        mid += rng.normal(0.0, 0.08)
        spread = 0.20 + 0.01 * int(rng.integers(0, 21))
        new_bid = round(mid - spread / 2, 2)
        new_ask = round(new_bid + spread, 2)
        kind = BOTH if i == 0 else int(rng.choice([BID, ASK, BOTH], p=[0.25, 0.25, 0.5]))
        if kind == BID and new_bid < ask:
            bid = new_bid
        elif kind == ASK and new_ask > bid:
            ask = new_ask
        else:
            kind, bid, ask = BOTH, new_bid, new_ask
        bids.append(bid)
        asks.append(ask)
        changed.append(kind)
    return pd.DataFrame({"ts_utc": times, "bid": bids, "ask": asks, "changed": changed})


def to_mt5_text(ticks: pd.DataFrame, *, line_end: str = "\n") -> str:
    """Render ticks as an MT5 tick export in NY+7 server time, writing only changed fields."""
    server = ticks["ts_utc"].dt.tz_convert(NEW_YORK).dt.tz_localize(None) + SERVER_SHIFT
    lines = [HEADER]
    for when, bid, ask, kind in zip(
        server, ticks["bid"], ticks["ask"], ticks["changed"], strict=True
    ):
        stamp = when.strftime("%Y.%m.%d\t%H:%M:%S.") + f"{when.microsecond // 1000:03d}"
        bid_text = f"{bid:.2f}" if kind in (BID, BOTH) else ""
        ask_text = f"{ask:.2f}" if kind in (ASK, BOTH) else ""
        lines.append(f"{stamp}\t{bid_text}\t{ask_text}\t\t\t{kind}")
    return line_end.join(lines) + line_end


def fixture_text(name: str) -> str:
    """The exact content of committed fixture `name`."""
    start, end, seed, price = FIXTURES[name]
    return to_mt5_text(synthetic_ticks(start, end, seed=seed, start_price=price))


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name in FIXTURES:
        (FIXTURE_DIR / name).write_text(fixture_text(name), encoding="utf-8")
        print(f"wrote {FIXTURE_DIR / name}")


if __name__ == "__main__":
    main()
