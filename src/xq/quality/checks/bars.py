"""Bar-level data-quality checks on each trading day's 1-minute bars (DQ-003)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.data.bars import BASES
from xq.quality.checks.common import utc, windows
from xq.quality.registry import Anomaly, Measurement, PartitionData, Scope, register

_MINUTE_NS = 60 * 1_000_000_000
_TOLERANCE = 1e-9
_MAD_TO_SIGMA = 1.4826


def _column(bars: pd.DataFrame, name: str) -> npt.NDArray[np.float64]:
    values: npt.NDArray[np.float64] = bars[name].to_numpy(dtype=np.float64)
    return values


def _bar_anomalies(bars: pd.DataFrame, mask: npt.NDArray[np.bool_], note: str) -> list[Anomaly]:
    return [Anomaly(utc(ts), 1.0, note) for ts in bars.loc[mask, "bar_start_utc"].to_numpy()]


def _session_minutes(data: PartitionData, sessions: list[str]) -> npt.NDArray[np.int64]:
    """Starts of the whole minutes inside the active-session windows."""
    minutes = [
        np.arange(-(-start // _MINUTE_NS) * _MINUTE_NS, end - _MINUTE_NS + 1, _MINUTE_NS)
        for start, end in windows(data, sessions)
    ]
    return np.concatenate(minutes).astype(np.int64) if minutes else np.array([], dtype=np.int64)


@register
class OhlcConsistency:
    """Bars whose high/low do not bound their open and close, for any price basis."""

    check_id = "bar.ohlc_consistency"
    scope = Scope.BAR
    description = "bars with high < max(open, close) or low > min(open, close) in any basis"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        bars = data.bars_1m
        if bars.empty:
            return None
        bad = np.zeros(len(bars), dtype=bool)
        for basis in BASES:
            o, h, lo, c = (_column(bars, f"{basis}_{p}") for p in ("open", "high", "low", "close"))
            bad |= (h < np.maximum(o, c) - _TOLERANCE) | (lo > np.minimum(o, c) + _TOLERANCE)
        return Measurement(
            float(bad.sum()), {"bars": len(bars)}, _bar_anomalies(bars, bad, "OHLC bounds")
        )


@register
class MissingMinutes:
    """Share of whole minutes in the active sessions that have no 1-minute bar."""

    check_id = "bar.missing_minutes"
    scope = Scope.BAR
    description = "minutes inside the active sessions without a 1-minute bar"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        expected = _session_minutes(data, params["active_sessions"])
        if len(expected) == 0:
            return None
        present = data.bars_1m["bar_start_utc"].to_numpy(dtype=np.int64)
        missing = expected[~np.isin(expected, present)]
        anomalies = []
        if len(missing):
            run_starts = np.flatnonzero(
                np.diff(missing, prepend=missing[0] - 2 * _MINUTE_NS) != _MINUTE_NS
            )
            run_ends = np.append(run_starts[1:], len(missing))
            anomalies = [
                Anomaly(utc(missing[s]), float(e - s), "consecutive missing minutes")
                for s, e in zip(run_starts, run_ends, strict=True)
            ]
        return Measurement(
            len(missing) / len(expected),
            {"expected_minutes": len(expected), "missing_minutes": len(missing)},
            anomalies,
        )


@register
class DuplicateBarStarts:
    """Number of 1-minute bars sharing a start with an earlier bar."""

    check_id = "bar.duplicate_starts"
    scope = Scope.BAR
    description = "1-minute bars with the same start as an earlier bar"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        bars = data.bars_1m
        if bars.empty:
            return None
        duplicated = bars["bar_start_utc"].duplicated(keep="first").to_numpy()
        return Measurement(
            float(duplicated.sum()),
            {"bars": len(bars)},
            _bar_anomalies(bars, duplicated, "duplicate start"),
        )


@register
class ExtremeReturns:
    """Number of 1-minute mid returns beyond `z` robust standard deviations of the day."""

    check_id = "bar.extreme_returns"
    scope = Scope.BAR
    description = (
        "close-to-close 1-minute mid returns between adjacent minutes beyond z robust sigma"
    )

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        bars = data.bars_1m
        if len(bars) < 3:
            return None
        starts = bars["bar_start_utc"].to_numpy(dtype=np.int64)
        adjacent = np.diff(starts) == _MINUTE_NS  # returns across gaps are not 1-minute returns
        returns = np.diff(np.log(_column(bars, "mid_close")))[adjacent]
        if len(returns) < 2:
            return None
        scale = _MAD_TO_SIGMA * float(np.median(np.abs(returns - np.median(returns))))
        if scale == 0:
            return None
        z = returns / scale
        extreme = np.abs(z) > float(params["z"])
        ends = starts[1:][adjacent]
        anomalies = [
            Anomaly(utc(t), float(v), "robust z of the 1-minute return")
            for t, v in zip(ends[extreme], z[extreme], strict=True)
        ]
        return Measurement(
            float(extreme.sum()), {"returns": len(returns), "robust_sigma": scale}, anomalies
        )


@register
class ZeroRangeBars:
    """Share of 1-minute bars in the active sessions whose mid never moved."""

    check_id = "bar.zero_range"
    scope = Scope.BAR
    description = "1-minute bars in the active sessions with mid high equal to mid low"

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        in_session = np.isin(
            data.bars_1m["bar_start_utc"].to_numpy(dtype=np.int64),
            _session_minutes(data, params["active_sessions"]),
        )
        bars = data.bars_1m.loc[in_session]
        if bars.empty:
            return None
        flat = _column(bars, "mid_high") - _column(bars, "mid_low") <= _TOLERANCE
        return Measurement(
            float(flat.mean()),
            {"bars": len(bars), "zero_range": int(flat.sum())},
            _bar_anomalies(bars, flat, "zero range"),
        )


@register
class BasisConsistency:
    """Bars whose bid, ask and mid disagree with each other."""

    check_id = "bar.basis_consistency"
    scope = Scope.BAR
    description = (
        "bars where ask < bid, mid is outside the bid/ask envelope or mid open/close != average"
    )

    def measure(self, data: PartitionData, params: Mapping[str, Any]) -> Measurement | None:
        bars = data.bars_1m
        if bars.empty:
            return None
        col = {name: _column(bars, name) for name in bars.columns if name.startswith(BASES)}
        bad = (
            (col["ask_close"] < col["bid_close"] - _TOLERANCE)
            | (col["ask_high"] < col["bid_high"] - _TOLERANCE)
            | (col["ask_low"] < col["bid_low"] - _TOLERANCE)
            | (col["mid_high"] > col["ask_high"] + _TOLERANCE)
            | (col["mid_low"] < col["bid_low"] - _TOLERANCE)
            | (np.abs(col["mid_open"] - (col["bid_open"] + col["ask_open"]) / 2) > _TOLERANCE)
            | (np.abs(col["mid_close"] - (col["bid_close"] + col["ask_close"]) / 2) > _TOLERANCE)
        )
        return Measurement(
            float(bad.sum()), {"bars": len(bars)}, _bar_anomalies(bars, bad, "bid/ask/mid mismatch")
        )
