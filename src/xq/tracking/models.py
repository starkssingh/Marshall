"""Metadata database models (DATA-005).

The metadata database records provenance: where every file came from, which run ingested it,
and — from Sprint 2 — how clean ticks, bars and spread statistics were derived; from Sprint 3 it
also holds vault gate tokens and every vault access (DS-004). It is SQLite in the
research tier and moves to PostgreSQL with paper trading (PAPER-004); the models use only portable
types.

Every instant is stored as UTC int64 nanoseconds (convention 1) through `NanoTimestamp`, which
accepts and returns tz-aware pandas timestamps and rejects naive ones. Exact decimals (contract
terms) are stored as text through `DecimalText`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Dialect,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from xq.core.time import from_ns, to_ns

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class NanoTimestamp(TypeDecorator[pd.Timestamp]):
    """UTC instant stored as int64 nanoseconds since the epoch."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(
        self, value: pd.Timestamp | datetime | None, dialect: Dialect
    ) -> int | None:
        return None if value is None else to_ns(value)

    def process_result_value(self, value: int | None, dialect: Dialect) -> pd.Timestamp | None:
        return None if value is None else from_ns(value)


class DecimalText(TypeDecorator[Decimal]):
    """Exact decimal stored as its canonical text (SQLite has no native decimal type)."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    """Declarative base with a naming convention so migrations get stable constraint names."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        pd.Timestamp: NanoTimestamp(),
        Decimal: DecimalText(),
        dict[str, Any]: JSON(),
        list[str]: JSON(),
    }


class DataSource(Base):
    """A declared data feed. Its clock convention is fixed for the life of the source id."""

    __tablename__ = "data_sources"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vendor: Mapped[str] = mapped_column(String(128))
    feed_type: Mapped[str] = mapped_column(String(32))
    venue: Mapped[str] = mapped_column(String(64))
    price_type: Mapped[str] = mapped_column(String(32))
    clock_convention: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")


class Instrument(Base):
    """Contract terms as configured when data for the instrument was first ingested."""

    __tablename__ = "instruments"

    instrument_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    tick_size: Mapped[Decimal]
    contract_size: Mapped[Decimal]
    quote_ccy: Mapped[str] = mapped_column(String(8))
    lot_step: Mapped[Decimal]
    min_lot: Mapped[Decimal]
    max_lot: Mapped[Decimal]
    venue: Mapped[str | None] = mapped_column(String(64))


class IngestRun(Base):
    """One execution of `xq ingest`."""

    __tablename__ = "ingest_runs"

    run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    started_at: Mapped[pd.Timestamp]
    finished_at: Mapped[pd.Timestamp | None]
    status: Mapped[str] = mapped_column(String(16))
    git_sha: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[dict[str, Any]]


class RawFile(Base):
    """An immutable source file in the raw store; this table is the raw-store manifest."""

    __tablename__ = "raw_files"

    raw_file_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    row_count: Mapped[int] = mapped_column(BigInteger)
    first_ts_utc: Mapped[pd.Timestamp | None]
    last_ts_utc: Mapped[pd.Timestamp | None]
    ingest_run_id: Mapped[str] = mapped_column(ForeignKey("ingest_runs.run_id"))
    ingested_at: Mapped[pd.Timestamp]


class CleanPartition(Base):
    """One trading day of clean ticks built with one cleaning-rules version (DATA-007)."""

    __tablename__ = "clean_partitions"

    partition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    trading_day: Mapped[date] = mapped_column(Date)
    rules_version: Mapped[str] = mapped_column(String(32))
    raw_file_ids: Mapped[list[str]]
    row_count: Mapped[int] = mapped_column(BigInteger)
    flagged_count: Mapped[int] = mapped_column(BigInteger)
    dropped_count: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))


class CleaningAction(Base):
    """A logged cleaning action with the original values it affected (DATA-007)."""

    __tablename__ = "cleaning_actions"

    action_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partition_id: Mapped[str] = mapped_column(ForeignKey("clean_partitions.partition_id"))
    rule_id: Mapped[str] = mapped_column(String(64))
    ts_utc: Mapped[pd.Timestamp]
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    original_values_json: Mapped[dict[str, Any]]


class BarSet(Base):
    """A built set of bars for one source, instrument, timeframe and price basis (DATA-008)."""

    __tablename__ = "bar_sets"

    bar_set_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    timeframe: Mapped[str] = mapped_column(String(8))
    basis: Mapped[str] = mapped_column(String(8))
    clean_rules_version: Mapped[str] = mapped_column(String(32))
    build_version: Mapped[str] = mapped_column(String(32))
    start_utc: Mapped[pd.Timestamp]
    end_utc: Mapped[pd.Timestamp]
    row_count: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))


class BarGap(Base):
    """An interval without bars; `expected_open` marks gaps while the market should be open."""

    __tablename__ = "bar_gaps"

    bar_set_id: Mapped[str] = mapped_column(ForeignKey("bar_sets.bar_set_id"), primary_key=True)
    gap_start_utc: Mapped[pd.Timestamp] = mapped_column(primary_key=True)
    gap_end_utc: Mapped[pd.Timestamp]
    expected_open: Mapped[bool] = mapped_column(Boolean)


class SpreadStat(Base):
    """Spread percentiles per hour of week, used by the cost-model fallback (DATA-009)."""

    __tablename__ = "spread_stats"

    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.instrument_id"), primary_key=True
    )
    hour_of_week: Mapped[int] = mapped_column(Integer, primary_key=True)
    computed_from: Mapped[pd.Timestamp] = mapped_column(primary_key=True)
    computed_to: Mapped[pd.Timestamp] = mapped_column(primary_key=True)
    p50: Mapped[float] = mapped_column(Float)
    p90: Mapped[float] = mapped_column(Float)
    p99: Mapped[float] = mapped_column(Float)
    n: Mapped[int] = mapped_column(BigInteger)


class QualityRunRecord(Base):
    """One `xq validate` run over a source and window (DQ-006)."""

    __tablename__ = "quality_runs"

    run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    start_utc: Mapped[pd.Timestamp]
    end_utc: Mapped[pd.Timestamp]
    rules_version: Mapped[str] = mapped_column(String(32))
    build_version: Mapped[str] = mapped_column(String(32))
    includes_vault: Mapped[bool] = mapped_column(Boolean)
    git_sha: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    report_path: Mapped[str] = mapped_column(Text)
    summary_json: Mapped[dict[str, Any]]
    created_at: Mapped[pd.Timestamp]


class QualityResultRecord(Base):
    """One graded check on one partition (trading day) of a quality run."""

    __tablename__ = "quality_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("quality_runs.run_id"), primary_key=True)
    partition_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    check_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trading_day: Mapped[date] = mapped_column(Date)
    severity: Mapped[str] = mapped_column(String(16))
    metric_value: Mapped[float] = mapped_column(Float)
    warn_threshold: Mapped[float | None] = mapped_column(Float)
    fail_threshold: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(8))
    details_json: Mapped[dict[str, Any]]


class VaultToken(Base):
    """A one-time gate token that unlocks the vault for one run (DS-004; issued by GATE-002).

    Only the SHA-256 of the secret is stored. A token is redeemed by the first run that uses it
    and refused for every other run.
    """

    __tablename__ = "vault_tokens"

    token_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_sha256: Mapped[str] = mapped_column(String(64))
    bundle_id: Mapped[str] = mapped_column(String(128))
    issued_by: Mapped[str] = mapped_column(String(128))
    issued_at: Mapped[pd.Timestamp]
    expires_at: Mapped[pd.Timestamp]
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    redeemed_at: Mapped[pd.Timestamp | None]
    redeemed_by_run: Mapped[str | None] = mapped_column(String(26))


class VaultAccess(Base):
    """One granted read of vault data: which token, run, purpose and window (DS-004)."""

    __tablename__ = "vault_access_log"

    access_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[str] = mapped_column(ForeignKey("vault_tokens.token_id"))
    run_id: Mapped[str] = mapped_column(String(26))
    purpose: Mapped[str] = mapped_column(Text)
    window_start_utc: Mapped[pd.Timestamp]
    window_end_utc: Mapped[pd.Timestamp]
    accessed_at: Mapped[pd.Timestamp]


class DatasetVersion(Base):
    """A materialized dataset: its resolved spec, inputs and content hash (DS-005)."""

    __tablename__ = "dataset_versions"

    dataset_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    spec_json: Mapped[dict[str, Any]]
    spec_hash: Mapped[str] = mapped_column(String(64))
    feature_set_version: Mapped[str] = mapped_column(String(64))
    target_set_version: Mapped[str | None] = mapped_column(String(64))
    bar_set_ids: Mapped[list[str]]
    quality_run_ids: Mapped[list[str]]
    start_utc: Mapped[pd.Timestamp]
    end_utc: Mapped[pd.Timestamp]
    row_count: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    git_sha: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[pd.Timestamp]


class Hypothesis(Base):
    """One version of a pre-registered hypothesis (EXP-001, EXP-002).

    The YAML text is stored with its SHA-256; an edited file becomes a new version and the previous
    one is marked superseded, so every version stays visible.
    """

    __tablename__ = "hypotheses"

    hypothesis_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    family_id: Mapped[str] = mapped_column(String(64))
    yaml_hash: Mapped[str] = mapped_column(String(64))
    yaml_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[pd.Timestamp]


class Experiment(Base):
    """An experiment testing one hypothesis version (EXP-001)."""

    __tablename__ = "experiments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["hypothesis_id", "hypothesis_version"],
            ["hypotheses.hypothesis_id", "hypotheses.version"],
            name="fk_experiments_hypothesis_hypotheses",
        ),
    )

    experiment_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(String(16))
    hypothesis_version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[pd.Timestamp]
    closed_at: Mapped[pd.Timestamp | None]


class Conclusion(Base):
    """The conclusion that closed an experiment (EXP-005): a verdict and five written fields."""

    __tablename__ = "conclusions"

    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.experiment_id"), primary_key=True
    )
    verdict: Mapped[str] = mapped_column(String(16))
    observed: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    interpretation: Mapped[str] = mapped_column(Text)
    limitations: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    created_at: Mapped[pd.Timestamp]


class Run(Base):
    """One execution inside an experiment, with everything needed to reproduce it (EXP-001/003)."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.experiment_id"))
    kind: Mapped[str] = mapped_column(String(32))
    confirmatory: Mapped[bool] = mapped_column(Boolean)
    git_sha: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[dict[str, Any]]
    dataset_id: Mapped[str | None] = mapped_column(String(32))
    lock_hash: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(BigInteger)
    host: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[pd.Timestamp]
    finished_at: Mapped[pd.Timestamp | None]
    status: Mapped[str] = mapped_column(String(16))


class Trial(Base):
    """One evaluated configuration, counted for multiple-testing corrections (EXP-001/004)."""

    __tablename__ = "trials"

    trial_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    family_id: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    evaluated_on_test: Mapped[bool] = mapped_column(Boolean)
    sharpe: Mapped[float | None] = mapped_column(Float)
    returns_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[pd.Timestamp]


class Metric(Base):
    """A named value logged by a run, optionally per walk-forward fold (EXP-001)."""

    __tablename__ = "metrics"

    metric_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    fold_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)


class Artifact(Base):
    """A file a run produced, with its SHA-256 (EXP-001)."""

    __tablename__ = "artifacts"

    artifact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    kind: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))


class FoldResultRecord(Base):
    """One walk-forward fold of one evaluation in a run (WF-002).

    `evaluation` names the model and target evaluated (a run may evaluate several); the selected
    parameters and the fold's metrics are JSON.
    """

    __tablename__ = "fold_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), primary_key=True)
    evaluation: Mapped[str] = mapped_column(String(128), primary_key=True)
    fold_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    train_start: Mapped[pd.Timestamp]
    train_end: Mapped[pd.Timestamp]
    test_start: Mapped[pd.Timestamp]
    test_end: Mapped[pd.Timestamp]
    params_json: Mapped[dict[str, Any]]
    metrics_json: Mapped[dict[str, Any]]


class BacktestRecord(Base):
    """One backtest of a strategy in a run (BT-010): its tier, versions, span, metrics and files.

    `tier` is ``vectorized`` (the screener) or ``event``; `ledger_path` is the decision ledger
    (event tier only) and `report_path` the report directory.
    """

    __tablename__ = "backtests"

    backtest_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    tier: Mapped[str] = mapped_column(String(16))
    strategy_id: Mapped[str] = mapped_column(String(128))
    strategy_version: Mapped[str] = mapped_column(String(64))
    cost_model_version: Mapped[str] = mapped_column(String(128))
    start: Mapped[pd.Timestamp]
    end: Mapped[pd.Timestamp]
    metrics_json: Mapped[dict[str, Any]]
    ledger_path: Mapped[str | None] = mapped_column(Text)
    report_path: Mapped[str] = mapped_column(Text)


class StatTestRecord(Base):
    """One statistical test of a validation run (Phase 17): the statistic, its p-value and the
    p-value adjusted within its test family (VAL-006); `params_json` says how it was run."""

    __tablename__ = "stat_tests"

    stat_test_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    test_name: Mapped[str] = mapped_column(String(160))
    family_id: Mapped[str] = mapped_column(String(64))
    statistic: Mapped[float | None] = mapped_column(Float)
    p_value: Mapped[float | None] = mapped_column(Float)
    adjusted_p: Mapped[float | None] = mapped_column(Float)
    params_json: Mapped[dict[str, Any]]


class RobustnessResultRecord(Base):
    """One robustness measure of a validation run (ROB-008): its parameters, metrics and whether
    its gate passed (null when it is reported, not gated)."""

    __tablename__ = "robustness_results"

    result_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    test_id: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[dict[str, Any]]
    metrics_json: Mapped[dict[str, Any]]
    passed: Mapped[bool | None] = mapped_column(Boolean)


class TargetSetRecord(Base):
    """The locked definition of a target set version (TGT-001).

    The first build that uses a target set version records its definition hash; a later build
    with a different definition under the same version is refused.
    """

    __tablename__ = "target_sets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    spec_json: Mapped[dict[str, Any]]
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[pd.Timestamp]


# --- model registry (MREG-001 ... MREG-005, Sprint 13) -----------------------------------------
# Enforcing triggers live in the migrations (0011 ...): versions and bundles are immutable,
# histories append-only, and a status moves only as `xq.registry.gates` allows (ADR 0060).


class ModelRecord(Base):
    """A named forecasting model (MREG-001); its fitted instances are `ModelVersionRecord`s."""

    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("name"),)

    model_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    task: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[pd.Timestamp]


class ModelVersionRecord(Base):
    """One fitted model: its artifact, data, target, training window, hyperparameters, metrics at
    registration, code and run, and its status (MREG-001). Only the status ever changes."""

    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("model_id", "version"),)

    model_version_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.model_id"))
    version: Mapped[int] = mapped_column(Integer)
    artifact_uri: Mapped[str] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[str | None] = mapped_column(String(32))
    feature_set_version: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64))
    train_window_json: Mapped[dict[str, Any]]
    hyperparams_json: Mapped[dict[str, Any]]
    metrics_snapshot_json: Mapped[dict[str, Any]]
    git_sha: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id"))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[pd.Timestamp]


class StatusHistoryRecord(Base):
    """One status change of a model version or a strategy bundle, with the gate record that
    allowed it (MREG-001, MREG-002). Append-only."""

    __tablename__ = "status_history"

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_kind: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    gate_result_id: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[pd.Timestamp]


class GateResultRecord(Base):
    """One evaluation of a gate (R1 ... R4) on a model version or a strategy bundle (MREG-002):
    the criteria, the measured values, whether it passed, the evaluator, the evidence files and
    the evidence policy's hash. Append-only; a promotion reads the latest one of its gate."""

    __tablename__ = "gate_results"

    gate_result_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_kind: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(64))
    gate: Mapped[str] = mapped_column(String(8))
    criteria_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    values_json: Mapped[dict[str, Any]]
    passed: Mapped[bool] = mapped_column(Boolean)
    evaluator: Mapped[str] = mapped_column(String(128))
    evidence_paths: Mapped[list[str]]
    gates_hash: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[str | None] = mapped_column(String(26))
    created_at: Mapped[pd.Timestamp]
