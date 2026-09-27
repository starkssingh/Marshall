"""Experiment registry API (EXP-001).

The registry records hypotheses (versioned), experiments, runs, metrics and artifacts; trials are
counted through `xq.tracking.trials` (EXP-004). Functions take an engine and return frozen
records, never live ORM objects.

The registry is **append-only**. Records are created and read, and a few fields move forward
through their lifecycle (a hypothesis version is superseded, a run finishes); nothing is ever
deleted. Deleting a trial or a failed run would make the trial count — and every multiple-testing
correction built on it — dishonest.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import pandas as pd
from sqlalchemy import Engine, func, select

from xq.core.errors import XQError
from xq.core.ids import new_ulid
from xq.core.time import utc_now
from xq.data.raw_store import sha256_file
from xq.tracking.db import session_factory
from xq.tracking.models import (
    Artifact,
    Experiment,
    FoldResultRecord,
    Hypothesis,
    Metric,
    Run,
)


class RegistryError(XQError):
    """A registry record is missing, or an operation violates its lifecycle."""


class HypothesisStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ExperimentStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class RunStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(frozen=True)
class HypothesisRef:
    """One registered version of a hypothesis."""

    hypothesis_id: str
    version: int
    title: str
    family_id: str
    yaml_hash: str
    status: HypothesisStatus
    created_at: pd.Timestamp


@dataclass(frozen=True)
class ExperimentRef:
    """An experiment on one hypothesis version."""

    experiment_id: str
    hypothesis_id: str
    hypothesis_version: int
    title: str
    status: ExperimentStatus
    verdict: str | None
    created_at: pd.Timestamp


@dataclass(frozen=True)
class RunRef:
    """A run and the provenance needed to reproduce it."""

    run_id: str
    experiment_id: str
    kind: str
    confirmatory: bool
    git_sha: str
    config_hash: str
    config: dict[str, Any]
    dataset_id: str | None
    lock_hash: str
    seed: int
    host: str
    started_at: pd.Timestamp
    finished_at: pd.Timestamp | None
    status: RunStatus


@dataclass(frozen=True)
class MetricRecord:
    run_id: str
    fold_id: str | None
    name: str
    value: float


@dataclass(frozen=True)
class ArtifactRecord:
    run_id: str
    kind: str
    path: str
    sha256: str


# --- hypotheses --------------------------------------------------------------------------------


def text_hash(text: str) -> str:
    """SHA-256 of a hypothesis document's exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_hypothesis_version(
    engine: Engine, hypothesis_id: str, *, title: str, family_id: str, yaml_text: str
) -> HypothesisRef:
    """Record `yaml_text` as the current version of `hypothesis_id`.

    If the latest version has the same text hash, it is returned unchanged. Otherwise a new version
    (latest + 1) is added and the previous one is marked superseded, so an edit is always visible.
    """
    digest = text_hash(yaml_text)
    with session_factory(engine)() as session:
        latest = session.scalars(
            select(Hypothesis)
            .where(Hypothesis.hypothesis_id == hypothesis_id)
            .order_by(Hypothesis.version.desc())
        ).first()
        if latest is not None and latest.yaml_hash == digest:
            return _hypothesis(latest)
        if latest is not None:
            latest.status = HypothesisStatus.SUPERSEDED.value
        record = Hypothesis(
            hypothesis_id=hypothesis_id,
            version=1 if latest is None else latest.version + 1,
            title=title,
            family_id=family_id,
            yaml_hash=digest,
            yaml_text=yaml_text,
            status=HypothesisStatus.ACTIVE.value,
            created_at=utc_now(),
        )
        session.add(record)
        session.commit()
        return _hypothesis(record)


def get_hypothesis(engine: Engine, hypothesis_id: str, version: int | None = None) -> HypothesisRef:
    """A hypothesis version (default: the latest)."""
    with session_factory(engine)() as session:
        query = select(Hypothesis).where(Hypothesis.hypothesis_id == hypothesis_id)
        if version is not None:
            query = query.where(Hypothesis.version == version)
        record = session.scalars(query.order_by(Hypothesis.version.desc())).first()
        if record is None:
            which = f" version {version}" if version is not None else ""
            raise RegistryError(f"hypothesis {hypothesis_id}{which} is not registered")
        return _hypothesis(record)


def hypothesis_text(engine: Engine, hypothesis_id: str, version: int) -> str:
    """The exact registered text of a hypothesis version."""
    with session_factory(engine)() as session:
        record = session.get(Hypothesis, (hypothesis_id, version))
        if record is None:
            raise RegistryError(f"hypothesis {hypothesis_id} version {version} is not registered")
        return record.yaml_text


def list_hypotheses(engine: Engine) -> list[HypothesisRef]:
    """Every version of every hypothesis, ordered by id and version."""
    with session_factory(engine)() as session:
        records = session.scalars(
            select(Hypothesis).order_by(Hypothesis.hypothesis_id, Hypothesis.version)
        ).all()
        return [_hypothesis(r) for r in records]


# --- experiments -------------------------------------------------------------------------------


def create_experiment(engine: Engine, hypothesis_id: str, title: str) -> ExperimentRef:
    """Open an experiment on the current version of `hypothesis_id`."""
    hypothesis = get_hypothesis(engine, hypothesis_id)
    with session_factory(engine)() as session:
        record = Experiment(
            experiment_id=new_ulid(),
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_version=hypothesis.version,
            title=title,
            status=ExperimentStatus.OPEN.value,
            verdict=None,
            created_at=utc_now(),
            closed_at=None,
        )
        session.add(record)
        session.commit()
        return _experiment(record)


def open_experiment_for(engine: Engine, hypothesis_id: str, title: str) -> ExperimentRef:
    """The latest open experiment on the current hypothesis version, created if there is none."""
    hypothesis = get_hypothesis(engine, hypothesis_id)
    with session_factory(engine)() as session:
        record = session.scalars(
            select(Experiment)
            .where(
                Experiment.hypothesis_id == hypothesis.hypothesis_id,
                Experiment.hypothesis_version == hypothesis.version,
                Experiment.status == ExperimentStatus.OPEN.value,
            )
            .order_by(Experiment.created_at.desc(), Experiment.experiment_id.desc())
        ).first()
        if record is not None:
            return _experiment(record)
    return create_experiment(engine, hypothesis_id, title)


def get_experiment(engine: Engine, experiment_id: str) -> ExperimentRef:
    with session_factory(engine)() as session:
        record = session.get(Experiment, experiment_id)
        if record is None:
            raise RegistryError(f"experiment {experiment_id} not found")
        return _experiment(record)


def list_experiments(engine: Engine, hypothesis_id: str | None = None) -> list[ExperimentRef]:
    with session_factory(engine)() as session:
        query = select(Experiment).order_by(Experiment.created_at, Experiment.experiment_id)
        if hypothesis_id is not None:
            query = query.where(Experiment.hypothesis_id == hypothesis_id)
        return [_experiment(r) for r in session.scalars(query).all()]


# --- runs, metrics, artifacts ------------------------------------------------------------------


def start_run(
    engine: Engine,
    experiment_id: str,
    *,
    kind: str,
    confirmatory: bool,
    git_sha: str,
    config_hash: str,
    config: dict[str, Any],
    dataset_id: str | None,
    lock_hash: str,
    seed: int,
    host: str,
    run_id: str | None = None,
) -> RunRef:
    """Record a new run in state ``running``."""
    experiment = get_experiment(engine, experiment_id)
    if experiment.status is not ExperimentStatus.OPEN:
        raise RegistryError(f"experiment {experiment_id} is {experiment.status}; open a new one")
    with session_factory(engine)() as session:
        record = Run(
            run_id=run_id or new_ulid(),
            experiment_id=experiment_id,
            kind=kind,
            confirmatory=confirmatory,
            git_sha=git_sha,
            config_hash=config_hash,
            config_json=config,
            dataset_id=dataset_id,
            lock_hash=lock_hash,
            seed=seed,
            host=host,
            started_at=utc_now(),
            finished_at=None,
            status=RunStatus.RUNNING.value,
        )
        session.add(record)
        session.commit()
        return _run(record)


def finish_run(engine: Engine, run_id: str, status: RunStatus) -> RunRef:
    """Move a running run to ``finished`` or ``failed`` (once)."""
    if status is RunStatus.RUNNING:
        raise RegistryError("a run can only finish as finished or failed")
    with session_factory(engine)() as session:
        record = _running(session.get(Run, run_id), run_id)
        record.status = status.value
        record.finished_at = utc_now()
        session.commit()
        return _run(record)


def get_run(engine: Engine, run_id: str) -> RunRef:
    with session_factory(engine)() as session:
        record = session.get(Run, run_id)
        if record is None:
            raise RegistryError(f"run {run_id} not found")
        return _run(record)


def list_runs(engine: Engine, experiment_id: str | None = None) -> list[RunRef]:
    with session_factory(engine)() as session:
        query = select(Run).order_by(Run.started_at, Run.run_id)
        if experiment_id is not None:
            query = query.where(Run.experiment_id == experiment_id)
        return [_run(r) for r in session.scalars(query).all()]


def log_metric(
    engine: Engine, run_id: str, name: str, value: float, *, fold_id: str | None = None
) -> None:
    """Record a metric of a running run.

    Raises:
        ValueError: if `value` is not finite (an undefined metric is not recorded at all).
    """
    if not math.isfinite(value):
        raise ValueError(
            f"metric {name!r} is not finite ({value}); undefined metrics are not logged"
        )
    with session_factory(engine)() as session:
        _running(session.get(Run, run_id), run_id)
        session.add(Metric(run_id=run_id, fold_id=fold_id, name=name, value=float(value)))
        session.commit()


class FoldSummary(Protocol):
    """A walk-forward fold as the registry stores it (see `xq.validation.walkforward`)."""

    @property
    def fold_id(self) -> str: ...
    @property
    def train_start(self) -> pd.Timestamp: ...
    @property
    def train_end(self) -> pd.Timestamp: ...
    @property
    def test_start(self) -> pd.Timestamp: ...
    @property
    def test_end(self) -> pd.Timestamp: ...
    @property
    def n_train(self) -> int: ...
    @property
    def n_val(self) -> int: ...
    @property
    def n_test(self) -> int: ...
    @property
    def selected(self) -> dict[str, Any]: ...
    @property
    def metrics(self) -> dict[str, float]: ...


@dataclass(frozen=True)
class FoldResultRow:
    """A stored ``fold_results`` row."""

    run_id: str
    evaluation: str
    fold_id: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    params: dict[str, Any]
    metrics: dict[str, Any]


def record_fold_results(
    engine: Engine, run_id: str, evaluation: str, folds: Sequence[FoldSummary]
) -> None:
    """Record every fold of one walk-forward evaluation of a running run (WF-002).

    Row counts go into the metrics; missing metric values are stored as null.
    """
    with session_factory(engine)() as session:
        _running(session.get(Run, run_id), run_id)
        for fold in folds:
            metrics = {
                "n_train": fold.n_train,
                "n_val": fold.n_val,
                "n_test": fold.n_test,
                **{k: _json_number(v) for k, v in fold.metrics.items()},
            }
            session.add(
                FoldResultRecord(
                    run_id=run_id,
                    evaluation=evaluation,
                    fold_id=fold.fold_id,
                    train_start=fold.train_start,
                    train_end=fold.train_end,
                    test_start=fold.test_start,
                    test_end=fold.test_end,
                    params_json=dict(fold.selected),
                    metrics_json=metrics,
                )
            )
        session.commit()


def get_fold_results(
    engine: Engine, run_id: str, evaluation: str | None = None
) -> list[FoldResultRow]:
    """Fold results of a run (optionally of one evaluation), by evaluation then fold."""
    with session_factory(engine)() as session:
        query = select(FoldResultRecord).where(FoldResultRecord.run_id == run_id)
        if evaluation is not None:
            query = query.where(FoldResultRecord.evaluation == evaluation)
        rows = session.scalars(
            query.order_by(FoldResultRecord.evaluation, FoldResultRecord.fold_id)
        ).all()
        return [
            FoldResultRow(
                r.run_id,
                r.evaluation,
                r.fold_id,
                r.train_start,
                r.train_end,
                r.test_start,
                r.test_end,
                r.params_json,
                r.metrics_json,
            )
            for r in rows
        ]


def _json_number(value: float) -> float | None:
    return None if math.isnan(value) else float(value)


def get_metrics(engine: Engine, run_id: str) -> list[MetricRecord]:
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(Metric).where(Metric.run_id == run_id).order_by(Metric.metric_id)
        ).all()
        return [MetricRecord(m.run_id, m.fold_id, m.name, m.value) for m in rows]


def log_artifact(engine: Engine, run_id: str, path: Path, *, kind: str) -> ArtifactRecord:
    """Record a file produced by a running run, with its SHA-256."""
    if not path.is_file():
        raise RegistryError(f"artifact {path} does not exist")
    digest = sha256_file(path)
    with session_factory(engine)() as session:
        _running(session.get(Run, run_id), run_id)
        session.add(Artifact(run_id=run_id, kind=kind, path=str(path), sha256=digest))
        session.commit()
    return ArtifactRecord(run_id, kind, str(path), digest)


def list_artifacts(engine: Engine, run_id: str) -> list[ArtifactRecord]:
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.artifact_id)
        ).all()
        return [ArtifactRecord(a.run_id, a.kind, a.path, a.sha256) for a in rows]


def count_runs(engine: Engine, *, experiment_id: str | None = None) -> int:
    with session_factory(engine)() as session:
        query = select(func.count()).select_from(Run)
        if experiment_id is not None:
            query = query.where(Run.experiment_id == experiment_id)
        return int(session.scalar(query) or 0)


# --- helpers -----------------------------------------------------------------------------------


def _running(record: Run | None, run_id: str) -> Run:
    if record is None:
        raise RegistryError(f"run {run_id} not found")
    if record.status != RunStatus.RUNNING.value:
        raise RegistryError(f"run {run_id} is {record.status}; it no longer accepts records")
    return record


def _hypothesis(r: Hypothesis) -> HypothesisRef:
    return HypothesisRef(
        r.hypothesis_id,
        r.version,
        r.title,
        r.family_id,
        r.yaml_hash,
        HypothesisStatus(r.status),
        r.created_at,
    )


def _experiment(r: Experiment) -> ExperimentRef:
    return ExperimentRef(
        r.experiment_id,
        r.hypothesis_id,
        r.hypothesis_version,
        r.title,
        ExperimentStatus(r.status),
        r.verdict,
        r.created_at,
    )


def _run(r: Run) -> RunRef:
    return RunRef(
        r.run_id,
        r.experiment_id,
        r.kind,
        r.confirmatory,
        r.git_sha,
        r.config_hash,
        dict(r.config_json),
        r.dataset_id,
        r.lock_hash,
        r.seed,
        r.host,
        r.started_at,
        r.finished_at,
        RunStatus(r.status),
    )
