"""Target framework (TGT-001).

A target set (``config/targets.yaml``, versioned) expands into `TargetSpec`s — one per horizon and
price reference — computed by its *kind* (TGT-002 adds ``forward_return``). Every target value at
decision time t comes with:

- ``label_start``: when the position would actually be entered (the execution time, at or after t);
- ``label_end``: the last instant whose data the value depends on, used for purging in
  walk-forward splits (a training label whose ``label_end`` reaches into a test fold leaks it);
- ``crosses_close``: whether the holding period from ``label_start`` to ``label_end`` spans a
  market close (False without a label; ADR 0026);
- ``scale``: the volatility scale used by vol-normalized variants (missing otherwise);
- ``fill_delay_s``: how late the later of the label's fills came after its intended fill time, in
  seconds (missing without a label); `fill_delay_report` summarizes it for build output.

Horizons are trading time: kinds receive a `MarketClock` and count only market-open time
(ADR 0026). Targets are stored apart from features, in long form (one row per decision time and
target). The schema guard `check_feature_matrix` refuses any target column in a feature matrix. A
target set's definition is hash-locked in ``target_sets`` the first time it is used: changing a
definition requires a new version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from xq.core.config import PriceRef, TargetSetConfig
from xq.core.errors import ConfigError, XQError
from xq.core.time import utc_now
from xq.data.calendar import MarketClock
from xq.tracking.db import session_factory
from xq.tracking.models import TargetSetRecord

#: Columns of a computed target (one target, indexed by decision time).
VALUE_COLUMNS = ("value", "label_start", "label_end", "crosses_close", "scale", "fill_delay_s")
#: Columns of the long target frame stored in ``targets.parquet``.
TARGET_FRAME_COLUMNS = ("target", *VALUE_COLUMNS)
#: Feature names may not start with these; they are reserved for targets.
RESERVED_PREFIXES = ("tgt_", "fwd_")


class TargetLeakError(XQError):
    """A feature matrix contains a target column (or a column reserved for targets)."""


class TargetSetChangedError(XQError):
    """A target set version is used with a definition different from the locked one."""


@dataclass(frozen=True)
class TargetSpec:
    """One target: its name, horizon, price reference and the kind's parameters."""

    name: str
    horizon: pd.Timedelta
    price_ref: PriceRef
    params: Mapping[str, Any]


@dataclass(frozen=True)
class Lookahead:
    """How far after a decision time a kind may read quotes (ADR 0026).

    Up to `market` of trading time (`MarketClock.advance`), then `wall` of clock time more (for
    example the allowed fill delay).
    """

    market: pd.Timedelta
    wall: pd.Timedelta


TargetFn = Callable[[TargetSpec, pd.DataFrame, pd.Series, MarketClock], pd.DataFrame]
SigmaFn = Callable[[pd.Series, TargetSetConfig, pd.Timedelta], pd.Series]


@dataclass(frozen=True)
class TargetKind:
    """How a kind of target is expanded and computed.

    Attributes:
        name: Kind name used in ``config/targets.yaml``.
        code_version: Part of every dataset id that uses the kind.
        expand: Target specs of a target set definition (validates its params).
        sigma: ``sigma(close, definition, bar)``: volatility rate per square-root minute at each
            decision time, from the base close series (indexed by decision time) and the base
            bar length; must be causal. A kind scales it to a horizon h by sqrt(h in minutes).
        compute: ``compute(spec, quotes, sigma, clock)`` returning `VALUE_COLUMNS` indexed by
            the decision times of `sigma`; `quotes` has ``ts_utc`` (tz-aware), ``bid`` and
            ``ask``; `clock` measures horizons in trading time and covers every decision time.
        lookahead: How far after a decision time the kind may read quotes.
    """

    name: str
    code_version: int
    expand: Callable[[TargetSetConfig], list[TargetSpec]]
    sigma: SigmaFn
    compute: TargetFn
    lookahead: Callable[[TargetSetConfig], Lookahead]


def definition_hash(definition: TargetSetConfig) -> str:
    """SHA-256 of a target set definition's canonical JSON."""
    canonical = json.dumps(
        definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lock_target_set(engine: Engine, name: str, version: str, definition: TargetSetConfig) -> str:
    """Record a target set version's definition on first use; refuse a changed definition.

    Returns:
        The definition hash.

    Raises:
        TargetSetChangedError: if the version is already locked with a different definition.
    """
    digest = definition_hash(definition)
    with session_factory(engine)() as session:
        record = session.get(TargetSetRecord, (name, version))
        if record is None:
            session.add(
                TargetSetRecord(
                    name=name,
                    version=version,
                    spec_json=definition.model_dump(mode="json"),
                    hash=digest,
                    created_at=utc_now(),
                )
            )
            session.commit()
        elif record.hash != digest:
            raise TargetSetChangedError(
                f"target set {name}.{version} was locked with a different definition; "
                "give the changed definition a new version"
            )
    return digest


def compute_targets(
    kind: TargetKind,
    specs: list[TargetSpec],
    quotes: pd.DataFrame,
    sigma: pd.Series,
    clock: MarketClock,
) -> pd.DataFrame:
    """All targets of a set in long form, indexed by decision time (sorted by time, then name)."""
    frames = []
    for spec in specs:
        values = kind.compute(spec, quotes, sigma, clock)
        missing = [c for c in VALUE_COLUMNS if c not in values.columns]
        if missing:
            raise ValueError(f"target kind {kind.name} returned no {missing} for {spec.name}")
        frame = values.loc[:, list(VALUE_COLUMNS)].copy()
        frame.insert(0, "target", spec.name)
        frames.append(frame)
    if not frames:
        raise ConfigError("a target set must define at least one target")
    combined = pd.concat(frames)
    combined.index.name = "decision_time"
    order = combined.reset_index().sort_values(["decision_time", "target"], kind="stable")
    return order.set_index("decision_time")


def target_values(targets: pd.DataFrame, name: str) -> pd.DataFrame:
    """The rows of one target from a long target frame, indexed by decision time."""
    one = targets[targets["target"] == name]
    if one.empty:
        known = ", ".join(sorted(targets["target"].unique())) or "none"
        raise KeyError(f"no target {name!r}; available: {known}")
    return one.drop(columns="target")


def fill_delay_report(targets: pd.DataFrame, threshold_s: float) -> dict[str, dict[str, Any]]:
    """Per target: labelled rows, rows with a fill later than `threshold_s`, the largest delay.

    A diagnostic of how often the allowed fill delay is used (ADR 0026); it does not change any
    value.
    """
    report: dict[str, dict[str, Any]] = {}
    for name, one in targets.groupby("target", sort=True):
        delays = one.loc[one["value"].notna(), "fill_delay_s"]
        report[str(name)] = {
            "labelled": len(delays),
            "delayed": int((delays > threshold_s).sum()),
            "max_delay_s": round(float(delays.max()), 3) if len(delays) else None,
        }
    return report


def check_feature_matrix(features: pd.DataFrame, target_names: Collection[str]) -> None:
    """Refuse feature matrices that contain target columns.

    A column is refused if it is a target name, a column of the target frame, or starts with a
    prefix reserved for targets.

    Raises:
        TargetLeakError: naming every offending column.
    """
    names = set(target_names) | set(TARGET_FRAME_COLUMNS)
    offending = sorted(
        str(c) for c in features.columns if str(c) in names or str(c).startswith(RESERVED_PREFIXES)
    )
    if offending:
        raise TargetLeakError(
            f"feature matrix contains target columns {offending}; targets are stored and joined "
            "separately (TGT-001)"
        )
