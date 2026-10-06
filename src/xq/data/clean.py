"""Non-destructive tick cleaning (DATA-007).

Cleaning reads the canonical view of the raw mirror one trading day at a time, applies versioned
rules that set bits in each tick's ``flags`` and writes a clean partition to
``data/clean/<source>/<instrument>/rules=<version>/year=YYYY/month=MM/day=DD/part.parquet``
(``day`` is the trading day). Nothing is repaired. Only exact duplicates, non-positive and crossed
quotes may be dropped, and only when the configuration says so. Every rule hit is logged in
``cleaning_actions`` with the tick's original values (drops always; flags unless disabled).

Rules, in the order they are evaluated (`RULES`):

- ``NONPOSITIVE`` — bid or ask <= 0; ``CROSSED`` — bid > ask;
- ``DUP_EXACT`` — same timestamp and quote as an earlier tick; ``DUP_TS_DIFF_PRICE`` — same
  timestamp, different quote;
- ``CLOSED_MARKET`` — outside the calendar's market hours for the trading day;
- ``SPREAD_OUTLIER`` — spread above a multiple of the median of the previous spreads;
- ``SPIKE`` — a whole-quote jump beyond a robust, time-scaled z threshold that reverts within a
  few ticks (ADR 0008);
- ``STALE`` — the quote repeats unchanged for longer than the stale limit.

All rules except ``SPIKE`` are *causal*: whether a tick is flagged depends only on that tick and
earlier ones. ``SPIKE`` is confirmed by later ticks, so it may describe data but must not decide
what a live system could have known (bars never exclude it; see `xq.core.config.BarsConfig`).
Rolling statistics run over *usable* ticks: known, positive, uncrossed and not exact duplicates.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from xq.core.config import AppConfig, CleaningConfig
from xq.core.errors import MirrorVersionError
from xq.core.logging import get_logger
from xq.core.time import from_ns, to_ns, trading_day
from xq.data.adapters.base import TICK_SCHEMA, validate_tick_frame
from xq.data.flags import FLAG_DTYPE, TickFlag
from xq.data.raw_store import MIRROR_DIR, MIRROR_VERSION, MIRROR_VERSION_KEY, sha256_file
from xq.data.sessions import build_session_table
from xq.tracking.db import session_factory
from xq.tracking.models import CleaningAction, CleanPartition, RawFile

#: Bump when rule logic changes; it is part of every rules version hash.
CLEAN_CODE_VERSION = 2
CLEAN_DIR = "clean"
RULES_VERSION_KEY = b"xq.rules_version"
_MAD_TO_SIGMA = 1.4826
_SORT_KEYS = ["ts_utc", "raw_file_id", "row_num"]

log = get_logger(__name__)


@dataclass(frozen=True)
class Rule:
    """A cleaning rule: the flag it sets, whether it is causal, and why a tick matches."""

    flag: TickFlag
    causal: bool
    reason: str


RULES: dict[str, Rule] = {
    "NONPOSITIVE": Rule(TickFlag.NONPOSITIVE, True, "bid or ask is not positive"),
    "CROSSED": Rule(TickFlag.CROSSED, True, "bid is above ask"),
    "DUP_EXACT": Rule(TickFlag.DUP_EXACT, True, "same timestamp and quote as an earlier tick"),
    "DUP_TS_DIFF_PRICE": Rule(
        TickFlag.DUP_TS_DIFF_PRICE, True, "same timestamp as an earlier tick, different quote"
    ),
    "CLOSED_MARKET": Rule(TickFlag.CLOSED_MARKET, True, "outside the calendar's market hours"),
    "SPREAD_OUTLIER": Rule(
        TickFlag.SPREAD_OUTLIER, True, "spread above the multiple of the trailing median spread"
    ),
    "SPIKE": Rule(
        TickFlag.SPIKE, False, "mid jump beyond the robust z threshold, reverted by later ticks"
    ),
    "STALE": Rule(TickFlag.STALE, True, "quote unchanged for longer than the stale limit"),
}

CLEANING_FLAGS = TickFlag.NONE
for _rule in RULES.values():
    CLEANING_FLAGS |= _rule.flag

ACTION_COLUMNS = ["rule_id", "ts_utc", "action", "reason", "original_values"]


@dataclass(frozen=True)
class MarketWindow:
    """Market hours of one trading day in UTC nanoseconds; ``None`` when the market is closed."""

    open_ns: int | None
    close_ns: int | None

    @property
    def is_open(self) -> bool:
        return self.open_ns is not None and self.close_ns is not None


@dataclass(frozen=True)
class CleaningOutcome:
    """Result of cleaning one partition."""

    ticks: pd.DataFrame
    actions: pd.DataFrame
    flagged: int
    dropped: int


@dataclass
class CleanBuildResult:
    """What one `build_clean` call did."""

    rules_version: str
    built: list[date] = field(default_factory=list)
    skipped: list[date] = field(default_factory=list)
    rows: int = 0
    flagged: int = 0
    dropped: int = 0


def rules_version(cfg: CleaningConfig, calendar: str) -> str:
    """``<label>-<hash>`` over the rule parameters, `CLEAN_CODE_VERSION`, the mirror schema and the
    calendar version, which decides ``CLOSED_MARKET`` (ADR 0070): a new calendar is a new store."""
    payload = json.dumps(
        {
            "code": CLEAN_CODE_VERSION,
            "mirror": MIRROR_VERSION,
            "rules": cfg.model_dump(mode="json"),
            "calendar": calendar,
        },
        sort_keys=True,
    )
    return f"{cfg.version}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def clean_rules_version(cfg: AppConfig) -> str:
    """The rules version of the configured cleaning rules on the configured calendar."""
    return rules_version(cfg.cleaning_config(), cfg.sessions_config().version)


def clean_ticks(ticks: pd.DataFrame, cfg: CleaningConfig, market: MarketWindow) -> CleaningOutcome:
    """Apply every rule to one partition of canonical ticks.

    The input is sorted by ``(ts_utc, raw_file_id, row_num)`` first, so the result does not
    depend on the order in which files were read.
    """
    frame = (
        ticks.sort_values(_SORT_KEYS, kind="stable", na_position="last")
        .reset_index(drop=True)
        .astype(TICK_SCHEMA)
    )
    ts = frame["ts_utc"].to_numpy(dtype=np.int64)
    bid = frame["bid"].to_numpy(dtype=np.float64)
    ask = frame["ask"].to_numpy(dtype=np.float64)
    base = frame["flags"].to_numpy(dtype=np.uint32)

    missing = ((base & np.uint32(TickFlag.MISSING_QUOTE)) != 0) | np.isnan(bid) | np.isnan(ask)
    known = ~missing
    masks: dict[str, npt.NDArray[np.bool_]] = {}
    details: dict[str, dict[int, dict[str, Any]]] = {name: {} for name in RULES}

    masks["NONPOSITIVE"] = known & ((bid <= 0) | (ask <= 0))
    masks["CROSSED"] = known & (bid > ask)
    exact = frame.duplicated(subset=["ts_utc", "bid", "ask", "bid_size", "ask_size"], keep="first")
    masks["DUP_EXACT"] = exact.to_numpy(dtype=bool)
    same_ts = frame.duplicated(subset=["ts_utc"], keep="first").to_numpy(dtype=bool)
    masks["DUP_TS_DIFF_PRICE"] = same_ts & ~masks["DUP_EXACT"]
    if market.is_open:
        masks["CLOSED_MARKET"] = (ts < market.open_ns) | (ts >= market.close_ns)
    else:
        masks["CLOSED_MARKET"] = np.ones(len(frame), dtype=bool)

    usable = known & ~masks["NONPOSITIVE"] & ~masks["CROSSED"] & ~masks["DUP_EXACT"]
    masks["SPREAD_OUTLIER"] = _spread_outliers(bid, ask, usable, cfg, details["SPREAD_OUTLIER"])
    spike_candidates = usable & ~masks["SPREAD_OUTLIER"]
    masks["SPIKE"] = _spikes(ts, bid, ask, spike_candidates, cfg, details["SPIKE"])
    masks["STALE"] = _stale(ts, bid, ask, usable, cfg, details["STALE"])

    flags = base.copy()
    for name, rule in RULES.items():
        flags[masks[name]] |= np.uint32(rule.flag)
    dropped = np.zeros(len(frame), dtype=bool)
    for name in cfg.drop:
        dropped |= masks[name]

    frame["flags"] = flags.astype(FLAG_DTYPE)
    actions = _actions(frame, masks, dropped, details, cfg)
    kept = frame.loc[~dropped].reset_index(drop=True)
    flagged = int(np.count_nonzero((kept["flags"].to_numpy() & np.uint32(CLEANING_FLAGS)) != 0))
    return CleaningOutcome(
        ticks=validate_tick_frame(kept),
        actions=actions,
        flagged=flagged,
        dropped=int(np.count_nonzero(dropped)),
    )


def build_clean(
    cfg: AppConfig,
    engine: Engine,
    source_id: str,
    *,
    start: date | None = None,
    end: date | None = None,
    force: bool = False,
) -> CleanBuildResult:
    """Clean every trading day of `source_id` that has ingested data (optionally within dates).

    A partition is rebuilt only if its contributing raw files changed, its file is missing or
    altered, or `force` is set; rebuilding the same inputs reproduces the same bytes.
    """
    source = cfg.source(source_id)
    cleaning = cfg.cleaning_config()
    version = rules_version(cleaning, cfg.sessions_config().version)
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    mirror_root = data_dir / MIRROR_DIR / source_id / source.instrument
    clean_root = data_dir / CLEAN_DIR / source_id / source.instrument / f"rules={version}"
    result = CleanBuildResult(rules_version=version)

    with session_factory(engine)() as session:
        records = list(session.scalars(select(RawFile).where(RawFile.source_id == source_id)))
        spans = [
            (r.raw_file_id, to_ns(r.first_ts_utc), to_ns(r.last_ts_utc))
            for r in records
            if r.first_ts_utc is not None and r.last_ts_utc is not None
        ]
        if not spans:
            return result
        first_day = trading_day(from_ns(min(s[1] for s in spans)))
        last_day = trading_day(from_ns(max(s[2] for s in spans)))
        first_day, last_day = max(first_day, start or first_day), min(last_day, end or last_day)
        if first_day > last_day:
            return result
        table = build_session_table(cfg.sessions_config(), first_day, last_day)

        days = zip(
            table["trading_day"].tolist(),
            _ns_list(table["day_start_utc"]),
            _ns_list(table["day_end_utc"]),
            _ns_list(table["market_open_utc"]),
            _ns_list(table["market_close_utc"]),
            strict=True,
        )
        for day, day_start, day_end, market_open, market_close in days:
            if day_start is None or day_end is None:  # pragma: no cover - bounds always exist
                continue
            contributing = sorted(rid for rid, lo, hi in spans if lo < day_end and hi >= day_start)
            if not contributing:
                continue
            partition_id = f"{source_id}:{source.instrument}:{version}:{day.isoformat()}"
            target = _partition_path(clean_root, day)
            existing = session.get(CleanPartition, partition_id)
            if (
                not force
                and existing is not None
                and existing.raw_file_ids == contributing
                and target.is_file()
                and sha256_file(target) == existing.sha256
            ):
                result.skipped.append(day)
                continue

            ticks = _read_mirror_day(mirror_root, day_start, day_end, contributing)
            if ticks.empty:
                continue
            outcome = clean_ticks(ticks, cleaning, MarketWindow(market_open, market_close))
            digest = _write_partition(outcome.ticks, target, version)
            _record_partition(
                session,
                partition_id,
                source_id,
                source.instrument,
                day,
                version,
                contributing,
                outcome,
                digest,
            )
            session.commit()
            result.built.append(day)
            result.rows += len(outcome.ticks)
            result.flagged += outcome.flagged
            result.dropped += outcome.dropped
            log.info(
                "clean_partition_built",
                trading_day=day.isoformat(),
                rows=len(outcome.ticks),
                flagged=outcome.flagged,
                dropped=outcome.dropped,
            )
    return result


def _ns_list(column: pd.Series) -> list[int | None]:
    values = column.to_numpy(dtype="datetime64[ns]").view(np.int64)
    missing = column.isna().to_numpy()
    return [None if gone else int(v) for v, gone in zip(values, missing, strict=True)]


def clean_partition_path(cfg: AppConfig, source_id: str, day: date, version: str) -> Path:
    """Where the clean partition of `day` lives for rules `version`."""
    source = cfg.source(source_id)
    root = cfg.paths.resolve(cfg.paths.data_dir) / CLEAN_DIR / source_id / source.instrument
    return _partition_path(root / f"rules={version}", day)


def _partition_path(clean_root: Path, day: date) -> Path:
    return (
        clean_root
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
        / "part.parquet"
    )


def _read_mirror_day(
    mirror_root: Path, day_start: int, day_end: int, raw_file_ids: Sequence[str]
) -> pd.DataFrame:
    columns = ["raw_file_id", "row_num", "ts_utc", "c_bid", "c_ask", "c_bid_size", "c_ask_size"]
    columns.append("c_flags")
    first = from_ns(day_start).date()
    last = from_ns(day_end - 1).date()
    frames = []
    utc_day = first
    while utc_day <= last:
        folder = (
            mirror_root
            / f"year={utc_day.year:04d}"
            / f"month={utc_day.month:02d}"
            / f"day={utc_day.day:02d}"
        )
        for raw_file_id in raw_file_ids:
            part = folder / f"part-{raw_file_id}.parquet"
            if not part.is_file():
                continue
            metadata = pq.read_schema(part).metadata or {}
            version = int(metadata.get(MIRROR_VERSION_KEY, b"1"))
            if version < MIRROR_VERSION:
                raise MirrorVersionError(
                    f"{part} has mirror schema v{version}, cleaning needs v{MIRROR_VERSION}; "
                    "run `xq rebuild-mirror --source <source>`"
                )
            frames.append(pq.read_table(part, columns=columns).to_pandas())
        utc_day += timedelta(days=1)
    if not frames:
        return pd.DataFrame({c: pd.Series(dtype=d) for c, d in TICK_SCHEMA.items()})
    mirror = pd.concat(frames, ignore_index=True)
    in_day = (mirror["ts_utc"] >= day_start) & (mirror["ts_utc"] < day_end)
    mirror = mirror.loc[in_day]
    ticks = pd.DataFrame(
        {
            "ts_utc": mirror["ts_utc"],
            "bid": mirror["c_bid"],
            "ask": mirror["c_ask"],
            "bid_size": mirror["c_bid_size"],
            "ask_size": mirror["c_ask_size"],
            "flags": mirror["c_flags"],
            "raw_file_id": mirror["raw_file_id"],
            "row_num": mirror["row_num"],
        }
    )
    return ticks.astype(TICK_SCHEMA).reset_index(drop=True)


def _write_partition(ticks: pd.DataFrame, target: Path, version: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(ticks, preserve_index=False)
    table = table.replace_schema_metadata(
        {**(table.schema.metadata or {}), RULES_VERSION_KEY: version.encode()}
    )
    temporary = target.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary, compression="zstd")
    os.replace(temporary, target)
    return sha256_file(target)


def _record_partition(
    session: Session,
    partition_id: str,
    source_id: str,
    instrument_id: str,
    day: date,
    version: str,
    raw_file_ids: list[str],
    outcome: CleaningOutcome,
    digest: str,
) -> None:
    values = {
        "source_id": source_id,
        "instrument_id": instrument_id,
        "trading_day": day,
        "rules_version": version,
        "raw_file_ids": raw_file_ids,
        "row_count": len(outcome.ticks),
        "flagged_count": outcome.flagged,
        "dropped_count": outcome.dropped,
        "sha256": digest,
    }
    existing = session.get(CleanPartition, partition_id)
    if existing is None:
        session.add(CleanPartition(partition_id=partition_id, **values))
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    session.flush()
    session.execute(delete(CleaningAction).where(CleaningAction.partition_id == partition_id))
    session.add_all(
        CleaningAction(
            partition_id=partition_id,
            rule_id=action["rule_id"],
            ts_utc=from_ns(int(action["ts_utc"])),
            action=action["action"],
            reason=action["reason"],
            original_values_json=action["original_values"],
        )
        for action in outcome.actions.to_dict("records")
    )


def _spread_outliers(
    bid: npt.NDArray[np.float64],
    ask: npt.NDArray[np.float64],
    usable: npt.NDArray[np.bool_],
    cfg: CleaningConfig,
    details: dict[int, dict[str, Any]],
) -> npt.NDArray[np.bool_]:
    rule = cfg.spread_outlier
    positions = np.flatnonzero(usable)
    spread = ask[positions] - bid[positions]
    median = (
        pd.Series(spread)
        .rolling(rule.window_ticks, min_periods=rule.min_periods)
        .median()
        .shift(1)
        .to_numpy()
    )
    with np.errstate(invalid="ignore"):
        hit = (median > 0) & (spread > rule.multiple * median)
    mask = np.zeros(len(bid), dtype=bool)
    mask[positions[hit]] = True
    for i in np.flatnonzero(hit):
        details[int(positions[i])] = {
            "spread": float(spread[i]),
            "trailing_median_spread": float(median[i]),
        }
    return mask


def _spikes(
    ts: npt.NDArray[np.int64],
    bid: npt.NDArray[np.float64],
    ask: npt.NDArray[np.float64],
    usable: npt.NDArray[np.bool_],
    cfg: CleaningConfig,
    details: dict[int, dict[str, Any]],
) -> npt.NDArray[np.bool_]:
    """Whole-quote jumps that revert (ADR 0008).

    The candidate return is the *common* move of bid and ask (both sides moving the same way,
    sized by the smaller move), so one-sided spread widening, e.g. at the rollover, is not a
    spike. It is scaled by the robust per-tick mid-return scale times sqrt(elapsed time / trailing
    median tick spacing), so a move across a pause is judged on the pause's length.
    """
    rule = cfg.spike
    positions = np.flatnonzero(usable)
    mask = np.zeros(len(bid), dtype=bool)
    if len(positions) < 2:
        return mask
    b, a, t = bid[positions], ask[positions], ts[positions].astype(np.float64)
    mid = (b + a) / 2
    bid_move, ask_move, mid_move = (_log_returns(v) for v in (b, a, mid))
    same_way = (np.sign(bid_move) == np.sign(ask_move)) & (bid_move != 0)
    common = np.where(same_way, np.sign(bid_move) * np.minimum(abs(bid_move), abs(ask_move)), 0.0)
    common[0] = np.nan

    scale = _trailing_median(np.abs(mid_move), rule.window_ticks, rule.min_periods)
    scale = np.maximum(scale * _MAD_TO_SIGMA, rule.min_scale_bps * 1e-4)
    elapsed = np.concatenate(([np.nan], np.diff(t)))
    spacing = _trailing_median(elapsed, rule.window_ticks, rule.min_periods)
    with np.errstate(invalid="ignore", divide="ignore"):
        time_factor = np.sqrt(np.maximum(1.0, elapsed / spacing))
        z = common / (scale * time_factor)
        candidates = np.flatnonzero(np.abs(z) > rule.z_threshold)

    resume_at = 0
    for c in (int(value) for value in candidates):
        if c < resume_at:
            continue
        before, jump = mid[c - 1], mid[c] - mid[c - 1]
        for j in range(c + 1, min(c + rule.reversal_ticks, len(mid) - 1) + 1):
            reverted = (mid[j] - mid[c]) * jump < 0 and abs(mid[j] - before) <= (
                1 - rule.reversal_fraction
            ) * abs(jump)
            if reverted:
                mask[positions[c:j]] = True
                for k in range(c, j):
                    details[int(positions[k])] = {
                        "z": float(z[c]),
                        "time_factor": float(time_factor[c]),
                        "reverted_after_ticks": j - c,
                    }
                resume_at = j + 1
                break
    return mask


def _log_returns(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    returns = np.empty(len(values))
    returns[0] = np.nan
    returns[1:] = np.diff(np.log(values))
    return returns


def _trailing_median(
    values: npt.NDArray[np.float64], window: int, min_periods: int
) -> npt.NDArray[np.float64]:
    """Median of the previous `window` values (the current one excluded)."""
    median: npt.NDArray[np.float64] = (
        pd.Series(values).rolling(window, min_periods=min_periods).median().shift(1).to_numpy()
    )
    return median


def _stale(
    ts: npt.NDArray[np.int64],
    bid: npt.NDArray[np.float64],
    ask: npt.NDArray[np.float64],
    usable: npt.NDArray[np.bool_],
    cfg: CleaningConfig,
    details: dict[int, dict[str, Any]],
) -> npt.NDArray[np.bool_]:
    positions = np.flatnonzero(usable)
    mask = np.zeros(len(ts), dtype=bool)
    if len(positions) == 0:
        return mask
    b, a, t = bid[positions], ask[positions], ts[positions]
    changed = np.ones(len(positions), dtype=bool)
    changed[1:] = (b[1:] != b[:-1]) | (a[1:] != a[:-1])
    # Time of the latest quote change at or before each tick (a past-only carry-forward).
    last_change = np.maximum.accumulate(np.where(changed, t, np.iinfo(np.int64).min))
    unchanged_ns = t - last_change
    hit = ~changed & (unchanged_ns > int(cfg.stale.seconds * 1e9))
    mask[positions[hit]] = True
    for i in np.flatnonzero(hit):
        details[int(positions[i])] = {"unchanged_seconds": float(unchanged_ns[i] / 1e9)}
    return mask


def _actions(
    frame: pd.DataFrame,
    masks: dict[str, npt.NDArray[np.bool_]],
    dropped: npt.NDArray[np.bool_],
    details: dict[str, dict[int, dict[str, Any]]],
    cfg: CleaningConfig,
) -> pd.DataFrame:
    ts = frame["ts_utc"].to_numpy(dtype=np.int64)
    prices = {
        c: frame[c].to_numpy(dtype=np.float64) for c in ("bid", "ask", "bid_size", "ask_size")
    }
    raw_file_ids = frame["raw_file_id"].tolist()
    row_nums = frame["row_num"].to_numpy(dtype=np.int64)
    records: list[dict[str, Any]] = []
    for name, rule in RULES.items():
        drops_here = name in cfg.drop
        for i in (int(value) for value in np.flatnonzero(masks[name])):
            action = "drop" if drops_here and dropped[i] else "flag"
            if action == "flag" and not cfg.log_flag_actions:
                continue
            original: dict[str, Any] = {c: _json_float(v[i]) for c, v in prices.items()}
            raw_file_id = raw_file_ids[i]
            original["raw_file_id"] = None if pd.isna(raw_file_id) else str(raw_file_id)
            original["row_num"] = int(row_nums[i])
            original.update(details[name].get(i, {}))
            records.append(
                {
                    "rule_id": name,
                    "ts_utc": int(ts[i]),
                    "action": action,
                    "reason": rule.reason,
                    "original_values": original,
                }
            )
    return pd.DataFrame(records, columns=ACTION_COLUMNS)


def _json_float(value: float) -> float | None:
    return None if np.isnan(value) else float(value)
