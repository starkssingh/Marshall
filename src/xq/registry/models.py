"""Models, model versions and statuses (MREG-001).

A **model** is a named forecasting method; a **model version** is one fitted instance of it: the
artifact it was saved as (with its SHA-256), the dataset and feature-set version it was trained
on, its target, training window, hyperparameters, the metrics recorded when it was registered, the
git sha and the run that produced it. A version is immutable: a refit is a new version.

Model versions and strategy bundles (`xq.registry.bundles`) carry a **status**::

    draft -> candidate -> validated -> vault_passed -> paper -> live_eligible -> live
    any status except retired -> retired

Every change is appended to ``status_history`` with who made it and why. A promotion needs a
passing record of the matching gate (`xq.registry.gates`, MREG-002). Retiring needs none. ``live``
is not reachable: it needs the GATE-004 human review, which is not built.

The database enforces the same rules with triggers (migrations 0011 onward), so a direct edit of a
status fails as well (ADR 0060).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, func, select

from xq.core.errors import XQError
from xq.core.ids import new_ulid
from xq.core.time import utc_now
from xq.data.raw_store import sha256_file
from xq.tracking.db import session_factory
from xq.tracking.models import ModelRecord, ModelVersionRecord, StatusHistoryRecord


class RegistryStateError(XQError):
    """A registry record is missing, or an operation breaks its lifecycle or its gates."""


class Status(StrEnum):
    """The lifecycle of a model version or a strategy bundle (module docstring)."""

    DRAFT = "draft"
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    VAULT_PASSED = "vault_passed"
    PAPER = "paper"
    LIVE_ELIGIBLE = "live_eligible"
    LIVE = "live"
    RETIRED = "retired"


class SubjectKind(StrEnum):
    """What a status, a gate result or a history row belongs to."""

    MODEL_VERSION = "model_version"
    BUNDLE = "bundle"


#: The promotion order; every status but ``retired`` may also be retired.
PROMOTIONS: Mapping[Status, Status] = {
    Status.DRAFT: Status.CANDIDATE,
    Status.CANDIDATE: Status.VALIDATED,
    Status.VALIDATED: Status.VAULT_PASSED,
    Status.VAULT_PASSED: Status.PAPER,
    Status.PAPER: Status.LIVE_ELIGIBLE,
    Status.LIVE_ELIGIBLE: Status.LIVE,
}


@dataclass(frozen=True)
class ModelRef:
    """A registered model."""

    model_id: str
    name: str
    task: str
    description: str
    created_at: pd.Timestamp


@dataclass(frozen=True)
class ModelVersionRef:
    """A registered model version (module docstring)."""

    model_version_id: str
    model_id: str
    model_name: str
    version: int
    artifact_uri: str
    artifact_sha256: str
    dataset_id: str | None
    feature_set_version: str
    target: str
    train_window: dict[str, Any]
    hyperparams: dict[str, Any]
    metrics_snapshot: dict[str, Any]
    git_sha: str
    run_id: str | None
    status: Status
    created_at: pd.Timestamp


@dataclass(frozen=True)
class StatusChange:
    """One row of the status history."""

    subject_kind: SubjectKind
    subject_id: str
    from_status: Status | None
    to_status: Status
    gate_result_id: int | None
    actor: str
    reason: str
    changed_at: pd.Timestamp


def register_model(engine: Engine, name: str, *, task: str, description: str) -> ModelRef:
    """Register a model name (idempotent for the same task).

    Raises:
        RegistryStateError: if the name is registered with another task.
    """
    with session_factory(engine)() as session:
        record = session.scalars(select(ModelRecord).where(ModelRecord.name == name)).first()
        if record is not None:
            if record.task != task:
                raise RegistryStateError(f"model {name!r} is registered for task {record.task!r}")
            return _model(record)
        record = ModelRecord(
            model_id=new_ulid(), name=name, task=task, description=description, created_at=utc_now()
        )
        session.add(record)
        session.commit()
        return _model(record)


def add_model_version(
    engine: Engine,
    model_name: str,
    *,
    artifact: Path,
    dataset_id: str | None,
    feature_set_version: str,
    target: str,
    train_window: Mapping[str, Any],
    hyperparams: Mapping[str, Any],
    metrics_snapshot: Mapping[str, Any],
    git_sha: str,
    run_id: str | None,
    actor: str,
) -> ModelVersionRef:
    """Register the next version of `model_name` in status ``draft``; the artifact's SHA-256 is
    recorded so a later load can check it.

    Raises:
        RegistryStateError: for an unknown model or a missing artifact.
    """
    if not artifact.is_file():
        raise RegistryStateError(f"model artifact {artifact} does not exist")
    digest = sha256_file(artifact)
    now = utc_now()
    with session_factory(engine)() as session:
        model = session.scalars(select(ModelRecord).where(ModelRecord.name == model_name)).first()
        if model is None:
            raise RegistryStateError(f"model {model_name!r} is not registered")
        latest = session.scalar(
            select(func.max(ModelVersionRecord.version)).where(
                ModelVersionRecord.model_id == model.model_id
            )
        )
        record = ModelVersionRecord(
            model_version_id=new_ulid(),
            model_id=model.model_id,
            version=(latest or 0) + 1,
            artifact_uri=str(artifact.resolve()),
            artifact_sha256=digest,
            dataset_id=dataset_id,
            feature_set_version=feature_set_version,
            target=target,
            train_window_json=dict(train_window),
            hyperparams_json=dict(hyperparams),
            metrics_snapshot_json=dict(metrics_snapshot),
            git_sha=git_sha,
            run_id=run_id,
            status=Status.DRAFT.value,
            created_at=now,
        )
        session.add(record)
        session.add(
            StatusHistoryRecord(
                subject_kind=SubjectKind.MODEL_VERSION.value,
                subject_id=record.model_version_id,
                from_status=None,
                to_status=Status.DRAFT.value,
                gate_result_id=None,
                actor=actor,
                reason="registered",
                changed_at=now,
            )
        )
        session.commit()
        return _version(record, model.name)


def get_model_version(engine: Engine, model_version_id: str) -> ModelVersionRef:
    """A model version by id.

    Raises:
        RegistryStateError: if it is not registered.
    """
    with session_factory(engine)() as session:
        record = session.get(ModelVersionRecord, model_version_id)
        if record is None:
            raise RegistryStateError(f"model version {model_version_id} is not registered")
        model = session.get(ModelRecord, record.model_id)
        assert model is not None  # the foreign key guarantees it
        return _version(record, model.name)


def list_model_versions(engine: Engine, model_name: str) -> list[ModelVersionRef]:
    """Every version of `model_name`, oldest first."""
    with session_factory(engine)() as session:
        model = session.scalars(select(ModelRecord).where(ModelRecord.name == model_name)).first()
        if model is None:
            return []
        rows = session.scalars(
            select(ModelVersionRecord)
            .where(ModelVersionRecord.model_id == model.model_id)
            .order_by(ModelVersionRecord.version)
        ).all()
        return [_version(r, model.name) for r in rows]


def status_history(engine: Engine, kind: SubjectKind, subject_id: str) -> list[StatusChange]:
    """Every status change of a subject, oldest first."""
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(StatusHistoryRecord)
            .where(
                StatusHistoryRecord.subject_kind == kind.value,
                StatusHistoryRecord.subject_id == subject_id,
            )
            .order_by(StatusHistoryRecord.history_id)
        ).all()
        return [
            StatusChange(
                SubjectKind(r.subject_kind),
                r.subject_id,
                None if r.from_status is None else Status(r.from_status),
                Status(r.to_status),
                r.gate_result_id,
                r.actor,
                r.reason,
                r.changed_at,
            )
            for r in rows
        ]


def _model(r: ModelRecord) -> ModelRef:
    return ModelRef(r.model_id, r.name, r.task, r.description, r.created_at)


def _version(r: ModelVersionRecord, name: str) -> ModelVersionRef:
    return ModelVersionRef(
        model_version_id=r.model_version_id,
        model_id=r.model_id,
        model_name=name,
        version=r.version,
        artifact_uri=r.artifact_uri,
        artifact_sha256=r.artifact_sha256,
        dataset_id=r.dataset_id,
        feature_set_version=r.feature_set_version,
        target=r.target,
        train_window=dict(r.train_window_json),
        hyperparams=dict(r.hyperparams_json),
        metrics_snapshot=dict(r.metrics_snapshot_json),
        git_sha=r.git_sha,
        run_id=r.run_id,
        status=Status(r.status),
        created_at=r.created_at,
    )
