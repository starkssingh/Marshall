"""Dataset builder, manifests and versioning (DS-005).

`build_dataset` turns a `DatasetSpec` into files under ``data/datasets/<dataset_id>/``:

- ``features.parquet`` — one row per complete base bar with ``start <= bar_start < end``, keyed by
  ``decision_time_utc`` (the bar's ``available_at``), computed by the spec's feature set;
- ``targets.parquet`` — if the spec names a target set (TGT-001), the targets of every decision time
  in long form (``target``, ``value``, ``label_start``, ``label_end``, ``scale``,
  ``fill_delay_s``), computed from clean ticks after the decision time; a schema guard keeps
  target columns out of the features;
- ``spec.yaml`` — the *resolved* spec (bar build, quality run and config digest pinned), from which
  the dataset can be rebuilt with the same id;
- ``manifest.json`` — row count, decision-time range, column types, per-file and combined SHA-256,
  git sha, code versions, bar sets, the quality run, the quality gate's decision (how many
  partitions were included, which carried warnings and which were excluded, with reasons and
  failing checks) and, with targets, the fill-delay diagnostic: per target, the labelled rows,
  those with a fill more than ``datasets.fill_delay_report_s`` late and the largest delay.

It also records a ``dataset_versions`` row. Data is read only through the catalog, so the vault is
enforced. Every trading day the inputs touch passes the quality gate (DQ-007): FAIL or unvalidated
days refuse the build unless the spec excludes them. Excluded days and incomplete bars are dropped
before features are computed.

Building the same resolved spec again reproduces the same bytes. If the dataset already exists,
the rebuild is compared with it: identical content is a no-op, different content raises
`DatasetIntegrityError` (the id no longer pins the data, which is a bug to investigate).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine, select

from xq.core.config import AppConfig
from xq.core.errors import ConfigError, XQError
from xq.core.ids import new_ulid
from xq.core.logging import get_logger
from xq.core.time import ensure_utc, trading_days, utc_now
from xq.data.bars import bar_set_id, build_version, exclude_mask
from xq.data.catalog import Catalog
from xq.data.clean import rules_version
from xq.data.raw_store import sha256_file
from xq.datasets.base_features import (
    BASE_INPUT,
    DECISION_TIME,
    FeatureContext,
    decision_index,
    feature_set,
)
from xq.datasets.spec import DatasetSpec, code_versions, dataset_id, dump_spec
from xq.datasets.vault import check_window, vault_start
from xq.quality.gate import GateDecision, gate_partitions
from xq.targets.base import (
    TargetKind,
    TargetSpec,
    check_feature_matrix,
    compute_targets,
    definition_hash,
    fill_delay_report,
    lock_target_set,
)
from xq.targets.kinds import target_kind
from xq.tracking.db import session_factory
from xq.tracking.models import DatasetVersion, QualityRunRecord

DATASETS_DIR = "datasets"
FEATURES_FILE = "features.parquet"
TARGETS_FILE = "targets.parquet"
SPEC_FILE = "spec.yaml"
MANIFEST_FILE = "manifest.json"
DECISION_COLUMN = "decision_time_utc"

log = get_logger(__name__)


class NoDatasetDataError(XQError):
    """The spec selects no bars, or no quality run covers its source and bar build."""


class DatasetIntegrityError(XQError):
    """A dataset's files differ from its manifest, or a rebuild differs from the stored dataset."""


@dataclass(frozen=True)
class DatasetRef:
    """A materialized dataset."""

    dataset_id: str
    path: Path
    spec: DatasetSpec
    manifest: dict[str, Any]
    reproduced: bool = False


def datasets_root(cfg: AppConfig) -> Path:
    """``<data_dir>/datasets``."""
    return cfg.paths.resolve(cfg.paths.data_dir) / DATASETS_DIR


def config_digest(cfg: AppConfig, spec: DatasetSpec) -> str:
    """Hash of the configuration the builder reads besides the stores.

    Covers the calendar, the instrument, the bar exclusion flags (which ticks targets may fill
    at) and the target set definition, if any.
    """
    payload: dict[str, Any] = {
        "sessions": cfg.sessions_config().model_dump(mode="json"),
        "instrument": cfg.instrument(spec.instrument).model_dump(mode="json"),
    }
    if spec.target_set is not None:
        definition = cfg.target_set(spec.target_set.name, spec.target_set.version)
        payload["target_set"] = definition_hash(definition)
        payload["fill_exclusions"] = cfg.bars_config().exclude_flags
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def code_versions_for(cfg: AppConfig, spec: DatasetSpec) -> dict[str, int]:
    """Code versions of the feature and target builders a spec uses."""
    versions = {f"features:{spec.feature_set}": feature_set(spec.feature_set).code_version}
    if spec.target_set is not None:
        definition = cfg.target_set(spec.target_set.name, spec.target_set.version)
        versions[f"targets:{spec.target_set}"] = target_kind(definition.kind).code_version
    return versions


def resolve_spec(cfg: AppConfig, engine: Engine, spec: DatasetSpec) -> DatasetSpec:
    """Pin the configured bar build, the quality run and the config digest.

    The quality run is the pinned one, or else the latest run of the source on the same bar
    build that overlaps the window and did not include vault days.

    Raises:
        NoDatasetDataError: if no such quality run exists (run `xq validate` first).
        ValueError: if the spec pins values that differ from what the stores provide.
    """
    build = build_version(cfg.bars_config(), rules_version(cfg.cleaning_config()))
    if spec.bar_build is not None and spec.bar_build != build:
        raise ValueError(
            f"spec pins bar_build={spec.bar_build!r}, but the configured bar build is {build!r}; "
            "build those bars or drop the pin"
        )
    window_start, window_end = spec.start - spec.warmup, spec.end
    with session_factory(engine)() as session:
        if spec.quality_run_id is not None:
            run = session.get(QualityRunRecord, spec.quality_run_id)
            if run is None or run.source_id != spec.source:
                raise NoDatasetDataError(
                    f"quality run {spec.quality_run_id} of {spec.source!r} not found"
                )
        else:
            run = session.scalars(
                select(QualityRunRecord)
                .where(
                    QualityRunRecord.source_id == spec.source,
                    QualityRunRecord.build_version == build,
                    QualityRunRecord.includes_vault.is_(False),
                    QualityRunRecord.start_utc < window_end,
                    QualityRunRecord.end_utc > window_start,
                )
                .order_by(QualityRunRecord.created_at.desc(), QualityRunRecord.run_id.desc())
            ).first()
            if run is None:
                raise NoDatasetDataError(
                    f"no quality run of {spec.source!r} on bar build {build} "
                    f"covers {window_start} to {window_end}; run `xq validate` first"
                )
        if run.build_version != build:
            raise ValueError(
                f"quality run {run.run_id} graded bar build {run.build_version}, not {build}"
            )
        run_id = run.run_id
    return spec.resolved(
        bar_build=build,
        quality_run_id=run_id,
        config_digest=config_digest(cfg, spec),
    )


def build_dataset(cfg: AppConfig, engine: Engine, spec: DatasetSpec, *, git_sha: str) -> DatasetRef:
    """Materialize `spec` (resolving it first) and record it; see the module docstring.

    Raises:
        VaultAccessError: if the window reaches past ``vault.start``.
        NoDatasetDataError: if the window holds no usable base bars or no quality run applies.
        DatasetIntegrityError: if the dataset exists with different content.
        ConfigError: for an unknown source, instrument mismatch or unknown feature set.
    """
    source = cfg.source(spec.source)
    if source.instrument != spec.instrument:
        raise ConfigError(
            f"source {spec.source!r} carries {source.instrument!r}, not {spec.instrument!r}"
        )
    check_window(cfg, spec.start, spec.end)  # research datasets never read the vault
    feature_def = feature_set(spec.feature_set)
    targets_def: tuple[TargetKind, list[TargetSpec]] | None = None
    lookahead = pd.Timedelta(0)
    if spec.target_set is not None:
        definition = cfg.target_set(spec.target_set.name, spec.target_set.version)
        kind = target_kind(definition.kind)
        targets_def = (kind, kind.expand(definition))
        lookahead = kind.lookahead(definition)
    resolved = resolve_spec(cfg, engine, spec)
    ds_id = dataset_id(resolved, code_versions_for(cfg, resolved))

    inputs, decision = _load_inputs(cfg, engine, resolved, lookahead)
    base = inputs[BASE_INPUT]
    context = FeatureContext(
        resolved.base_timeframe, tuple(resolved.context_timeframes), cfg.sessions_config()
    )
    features = feature_def.compute(inputs, context)
    in_window = (base["bar_start_utc"] >= resolved.start).to_numpy()
    features = features.loc[in_window]
    if features.empty:
        raise NoDatasetDataError(
            f"no complete {resolved.base_timeframe.value} bars of {resolved.source!r} between "
            f"{resolved.start} and {resolved.end} (after exclusions)"
        )
    target_names = [t.name for t in targets_def[1]] if targets_def else []
    check_feature_matrix(features, target_names)
    targets: pd.DataFrame | None = None
    if targets_def is not None and resolved.target_set is not None:
        definition = cfg.target_set(resolved.target_set.name, resolved.target_set.version)
        lock_target_set(engine, resolved.target_set.name, resolved.target_set.version, definition)
        excluded = {e.trading_day for e in decision.excluded}
        decisions = pd.DatetimeIndex(features.index)
        targets = _targets(cfg, resolved, base, decisions, excluded, *targets_def, definition)

    root = datasets_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".tmp-{ds_id}-{new_ulid()}"
    staging.mkdir()
    try:
        _write_frame(features, staging / FEATURES_FILE)
        if targets is not None:
            _write_frame(targets, staging / TARGETS_FILE)
        (staging / SPEC_FILE).write_text(dump_spec(resolved), encoding="utf-8")
        manifest = _manifest(cfg, ds_id, resolved, features, targets, decision, staging, git_sha)
        (staging / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n")
        final = root / ds_id
        reproduced = final.exists()
        if reproduced:
            existing = read_manifest(cfg, ds_id)
            _verify_files(final, existing)
            if existing["sha256"] != manifest["sha256"]:
                raise DatasetIntegrityError(
                    f"rebuilding {ds_id} produced content {manifest['sha256'][:12]}, but the "
                    f"stored dataset has {existing['sha256'][:12]}; the id no longer pins the data"
                )
            manifest = existing
        else:
            staging.rename(final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    _record(engine, ds_id, resolved, manifest, final)
    if "fill_delays" in manifest:
        delays = manifest["fill_delays"]
        log.info(
            "target_fill_delays",
            dataset_id=ds_id,
            threshold_s=delays["threshold_s"],
            delayed={n: r["delayed"] for n, r in delays["targets"].items()},
        )
    log.info(
        "dataset_reproduced" if reproduced else "dataset_built",
        dataset_id=ds_id,
        rows=manifest["row_count"],
        sha256=manifest["sha256"],
    )
    return DatasetRef(ds_id, final, resolved, manifest, reproduced)


def read_manifest(cfg: AppConfig, ds_id: str) -> dict[str, Any]:
    """The manifest of a materialized dataset.

    Raises:
        NoDatasetDataError: if the dataset does not exist.
    """
    path = datasets_root(cfg) / ds_id / MANIFEST_FILE
    if not path.is_file():
        raise NoDatasetDataError(f"dataset {ds_id} not found under {path.parent.parent}")
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return manifest


def verify_dataset(cfg: AppConfig, ds_id: str) -> dict[str, Any]:
    """Re-hash every file of a dataset against its manifest; return the manifest.

    Raises:
        NoDatasetDataError: if the dataset does not exist.
        DatasetIntegrityError: if a file is missing or differs.
    """
    manifest = read_manifest(cfg, ds_id)
    _verify_files(datasets_root(cfg) / ds_id, manifest)
    return manifest


def load_dataset(
    cfg: AppConfig, ds_id: str, part: Literal["features", "targets"] = "features"
) -> pd.DataFrame:
    """One part of a dataset, indexed by tz-aware decision time, after verifying its hash.

    Raises:
        NoDatasetDataError: if the dataset or part does not exist.
        DatasetIntegrityError: if the file differs from the manifest.
    """
    manifest = read_manifest(cfg, ds_id)
    name = FEATURES_FILE if part == "features" else TARGETS_FILE
    expected = manifest["files"].get(name)
    path = datasets_root(cfg) / ds_id / name
    if expected is None or not path.is_file():
        raise NoDatasetDataError(f"dataset {ds_id} has no {part}")
    if sha256_file(path) != expected:
        raise DatasetIntegrityError(f"{path} differs from the manifest of {ds_id}")
    frame = pd.read_parquet(path)
    return frame.set_index(pd.DatetimeIndex(frame.pop(DECISION_COLUMN), name=DECISION_TIME))


def _verify_files(directory: Path, manifest: dict[str, Any]) -> None:
    for name, expected in manifest["files"].items():
        path = directory / name
        if not path.is_file() or sha256_file(path) != expected:
            raise DatasetIntegrityError(
                f"{path} is missing or differs from the manifest of {manifest['dataset_id']}"
            )


def _load_inputs(
    cfg: AppConfig, engine: Engine, spec: DatasetSpec, lookahead: pd.Timedelta
) -> tuple[dict[str, pd.DataFrame], GateDecision]:
    """Complete bars of every input timeframe, gated by the spec's quality run (DQ-007).

    With targets, the trading days their quotes may reach (up to `lookahead` after the last
    decision, never past the vault) are gated too.
    """
    catalog = Catalog(cfg)
    load_start = ensure_utc(spec.start - spec.warmup)
    starts = {spec.base_timeframe: load_start}
    for tf in spec.context_timeframes:
        # Start one context bar earlier so one is already available when the window opens.
        starts[tf] = load_start - tf.duration
    loaded: dict[str, pd.DataFrame] = {}
    for tf, start in starts.items():
        bars = catalog.load_bars(
            spec.source,
            spec.instrument,
            tf,
            spec.price_basis,
            start,
            spec.end,
            build=spec.bar_build,
        )
        name = BASE_INPUT if tf == spec.base_timeframe else tf.value
        loaded[name] = bars.loc[bars["is_complete"].to_numpy()].reset_index(drop=True)

    exclusions = {e.trading_day: e.reason for e in spec.exclusions}
    days = {day for bars in loaded.values() for day in bars["trading_day"]} | set(exclusions)
    if lookahead > pd.Timedelta(0):
        ahead_end = min(
            ensure_utc(spec.end) + spec.base_timeframe.duration + lookahead, vault_start(cfg)
        )
        if ahead_end > ensure_utc(spec.end):
            ahead = catalog.load_bars(
                spec.source,
                spec.instrument,
                spec.base_timeframe,
                spec.price_basis,
                spec.end,
                ahead_end,
                build=spec.bar_build,
            )
            days |= set(ahead["trading_day"])
    if spec.quality_run_id is None:
        raise ValueError("the spec must be resolved before its inputs are gated")
    decision = gate_partitions(engine, spec.quality_run_id, days, exclusions)
    excluded = {e.trading_day for e in decision.excluded}
    inputs = {
        name: bars.loc[~bars["trading_day"].isin(excluded).to_numpy()].reset_index(drop=True)
        for name, bars in loaded.items()
    }
    return inputs, decision


def _targets(
    cfg: AppConfig,
    spec: DatasetSpec,
    base: pd.DataFrame,
    decisions: pd.DatetimeIndex,
    excluded: set[Any],
    kind: TargetKind,
    specs: list[TargetSpec],
    definition: Any,
) -> pd.DataFrame:
    """Targets of every decision time, computed month by month from clean ticks.

    Sigma-hat is computed on the gated base bars (warm-up included) and taken at each decision
    time. Quotes are the clean ticks from the decision time up to `lookahead` later (never past
    the vault), without ticks the bars exclude and without ticks of excluded trading days.
    """
    close = pd.Series(base["close"].to_numpy(), index=decision_index(base))
    sigma = kind.sigma(close, definition, spec.base_timeframe.duration).reindex(decisions)
    lookahead = kind.lookahead(definition)
    vault = vault_start(cfg)
    mask = exclude_mask(cfg.bars_config())
    catalog = Catalog(cfg)
    months = decisions.tz_convert("UTC").strftime("%Y-%m")
    frames = []
    for _, chunk in sigma.groupby(months, sort=True):
        start = chunk.index[0]
        end = min(chunk.index[-1] + lookahead + pd.Timedelta(seconds=1), vault)
        if end <= start:  # decisions at the vault start: every fill would need vault quotes
            quotes = pd.DataFrame(
                {
                    "ts_utc": pd.Series(dtype="datetime64[ns, UTC]"),
                    "bid": pd.Series(dtype="float64"),
                    "ask": pd.Series(dtype="float64"),
                }
            )
        else:
            ticks = catalog.load_ticks(spec.source, spec.instrument, start, end)
            usable = (ticks["flags"].to_numpy() & mask) == 0
            if excluded and len(ticks):
                days = trading_days(pd.DatetimeIndex(ticks["ts_utc"]))
                usable &= ~pd.Series([d.item() for d in days]).isin(excluded).to_numpy()
            quotes = ticks.loc[usable, ["ts_utc", "bid", "ask"]].reset_index(drop=True)
        frames.append(compute_targets(kind, specs, quotes, chunk))
    return pd.concat(frames)


def _write_frame(frame: pd.DataFrame, path: Path) -> None:
    table = frame.reset_index().rename(columns={DECISION_TIME: DECISION_COLUMN})
    pq.write_table(pa.Table.from_pandas(table, preserve_index=False), path, compression="zstd")


def _manifest(
    cfg: AppConfig,
    ds_id: str,
    spec: DatasetSpec,
    features: pd.DataFrame,
    targets: pd.DataFrame | None,
    decision: GateDecision,
    directory: Path,
    git_sha: str,
) -> dict[str, Any]:
    files = {FEATURES_FILE: sha256_file(directory / FEATURES_FILE)}
    columns = {"features": {str(c): str(t) for c, t in features.dtypes.items()}}
    target_info: dict[str, Any] = {}
    if targets is not None and spec.target_set is not None:
        files[TARGETS_FILE] = sha256_file(directory / TARGETS_FILE)
        columns["targets"] = {str(c): str(t) for c, t in targets.dtypes.items()}
        definition = cfg.target_set(spec.target_set.name, spec.target_set.version)
        threshold = cfg.datasets_config().fill_delay_report_s
        target_info = {
            "targets": sorted(targets["target"].unique().tolist()),
            "target_set_hash": definition_hash(definition),
            "target_rows": len(targets),
            "fill_delays": {
                "threshold_s": threshold,
                "targets": fill_delay_report(targets, threshold),
            },
        }
    combined = hashlib.sha256(
        "".join(f"{name}:{digest}\n" for name, digest in sorted(files.items())).encode()
    ).hexdigest()
    timeframes = [spec.base_timeframe, *spec.context_timeframes]
    build = spec.bar_build or ""
    return {
        "dataset_id": ds_id,
        "name": spec.name,
        "spec": spec.model_dump(mode="json"),
        "code_versions": {**code_versions(), **code_versions_for(cfg, spec)},
        "row_count": len(features),
        "decision_time_first": str(features.index[0]),
        "decision_time_last": str(features.index[-1]),
        "columns": columns,
        **target_info,
        "files": files,
        "sha256": combined,
        "bar_set_ids": [bar_set_id(spec.source, spec.instrument, tf, build) for tf in timeframes],
        "quality_run_ids": [spec.quality_run_id],
        "included_partitions": len(decision.included),
        "warn_partitions": decision.manifest_entry()["warn_partitions"],
        "excluded_partitions": decision.manifest_entry()["excluded_partitions"],
        "git_sha": git_sha,
        "created_at": str(utc_now()),
    }


def _record(
    engine: Engine, ds_id: str, spec: DatasetSpec, manifest: dict[str, Any], path: Path
) -> None:
    with session_factory(engine)() as session:
        existing = session.get(DatasetVersion, ds_id)
        if existing is not None:
            if existing.sha256 != manifest["sha256"]:
                raise DatasetIntegrityError(
                    f"dataset_versions records {ds_id} with a different sha256"
                )
            return
        session.add(
            DatasetVersion(
                dataset_id=ds_id,
                name=spec.name,
                spec_json=manifest["spec"],
                spec_hash=hashlib.sha256(spec.canonical_json().encode()).hexdigest(),
                feature_set_version=str(spec.feature_set),
                target_set_version=str(spec.target_set) if spec.target_set else None,
                bar_set_ids=manifest["bar_set_ids"],
                quality_run_ids=manifest["quality_run_ids"],
                start_utc=pd.Timestamp(spec.start),
                end_utc=pd.Timestamp(spec.end),
                row_count=manifest["row_count"],
                sha256=manifest["sha256"],
                git_sha=manifest["git_sha"],
                path=str(path),
                created_at=utc_now(),
            )
        )
        session.commit()
