"""Content-hashed strategy bundles (MREG-003).

A **strategy bundle** is everything a runtime needs to trade a strategy, and nothing else:

- the model versions it uses, pinned by id and artifact SHA-256 (none for a rule baseline);
- the feature-set version, the data source, instrument, base timeframe and price basis;
- the strategy's configuration (a rule baseline's rule and volatility target, or later a signal
  engine strategy);
- the risk configuration (the whole risk profile);
- the cost-model version (venue and configuration hash).

Its id is the SHA-256 of its canonical JSON, so **the same inputs always give the same id** and a
changed input gives another bundle. Registering the same content again returns the bundle already
registered. The content is immutable in the database (migration 0013) and `load_bundle` checks
that the stored content still hashes to its id: runtimes load only intact bundles. The origin (the
run and strategy a bundle was built from) is provenance, not content: it is where the gate
evaluator finds the evidence (GATE-001).

`bundle_from_board_run` builds the bundle of a rule baseline of a baseline board run. A
forecast-sign strategy needs registered model versions (ML-009) and is refused until they exist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select

from xq.backtest.costs import CostModel
from xq.backtest.report import cost_model_version
from xq.core.config import AppConfig
from xq.core.time import utc_now
from xq.datasets.builder import recorded_spec
from xq.models.board import BoardConfig
from xq.registry.models import RegistryStateError, Status, SubjectKind
from xq.tracking import registry
from xq.tracking.db import session_factory
from xq.tracking.models import StatusHistoryRecord, StrategyBundleRecord

BOARD_KIND = "baseline_board"


class BundleIntegrityError(RegistryStateError):
    """A stored bundle's content no longer hashes to its id."""


class ModelVersionPin(BaseModel):
    """A model version a bundle uses, pinned by its artifact's hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_version_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BundleStrategy(BaseModel):
    """The strategy a bundle trades."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["baseline_rule"]
    name: str
    #: The rule (``rule``, ``params``, ``vol_target``) and the volatility target it uses, if any.
    config: dict[str, Any]
    signal_timeframe: str


class StrategyBundle(BaseModel):
    """A bundle's content (module docstring); its hash is its id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    instrument: str
    source: str
    base_timeframe: str
    price_basis: str
    feature_set_version: str
    model_versions: list[ModelVersionPin] = []
    strategy: BundleStrategy
    risk_config: dict[str, Any]
    cost_model_version: str

    def canonical_json(self) -> str:
        """The content as canonical JSON (sorted keys, no whitespace)."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def bundle_id(self) -> str:
        """The SHA-256 of the canonical JSON: the bundle's id."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BundleRef:
    """A registered bundle."""

    bundle_id: str
    name: str
    content: StrategyBundle
    origin_run_id: str | None
    origin_strategy: str | None
    status: Status
    created_at: pd.Timestamp

    @property
    def short_id(self) -> str:
        """The first 12 hex digits, for display."""
        return self.bundle_id[:12]


def register_bundle(
    engine: Engine,
    content: StrategyBundle,
    *,
    name: str,
    origin_run_id: str | None,
    origin_strategy: str | None,
    actor: str,
) -> BundleRef:
    """Register a bundle as draft, or return the bundle already registered with this content."""
    bundle_id = content.bundle_id()
    with session_factory(engine)() as session:
        record = session.get(StrategyBundleRecord, bundle_id)
        if record is not None:
            return _bundle(record)
        now = utc_now()
        record = StrategyBundleRecord(
            bundle_id=bundle_id,
            name=name,
            content_json=content.model_dump(mode="json"),
            origin_run_id=origin_run_id,
            origin_strategy=origin_strategy,
            status=Status.DRAFT.value,
            created_at=now,
        )
        session.add(record)
        session.add(
            StatusHistoryRecord(
                subject_kind=SubjectKind.BUNDLE.value,
                subject_id=bundle_id,
                from_status=None,
                to_status=Status.DRAFT.value,
                gate_result_id=None,
                actor=actor,
                reason="registered",
                changed_at=now,
            )
        )
        session.commit()
        return _bundle(record)


def resolve_bundle_id(engine: Engine, prefix: str) -> str:
    """The id of the one bundle whose id starts with `prefix` (at least 8 hex digits).

    Raises:
        RegistryStateError: for a short prefix, or none or several matching bundles.
    """
    if len(prefix) < 8:
        raise RegistryStateError("a bundle id prefix needs at least 8 hex digits")
    with session_factory(engine)() as session:
        ids = session.scalars(
            select(StrategyBundleRecord.bundle_id).where(
                StrategyBundleRecord.bundle_id.startswith(prefix.lower())
            )
        ).all()
    if len(ids) != 1:
        raise RegistryStateError(f"{len(ids)} bundles match {prefix!r}; expected one")
    return ids[0]


def get_bundle(engine: Engine, bundle_id: str) -> BundleRef:
    """A registered bundle (by full id or unique prefix).

    Raises:
        RegistryStateError: if it is not registered.
    """
    full = bundle_id if len(bundle_id) == 64 else resolve_bundle_id(engine, bundle_id)
    with session_factory(engine)() as session:
        record = session.get(StrategyBundleRecord, full)
        if record is None:
            raise RegistryStateError(f"bundle {bundle_id} is not registered")
        return _bundle(record)


def list_bundles(engine: Engine) -> list[BundleRef]:
    """Every registered bundle, oldest first."""
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(StrategyBundleRecord).order_by(StrategyBundleRecord.created_at)
        ).all()
        return [_bundle(r) for r in rows]


def load_bundle(engine: Engine, bundle_id: str) -> StrategyBundle:
    """The content of a bundle, after checking it still hashes to its id (module docstring).

    Raises:
        RegistryStateError: if it is not registered.
        BundleIntegrityError: if its stored content no longer hashes to its id.
    """
    ref = get_bundle(engine, bundle_id)
    if ref.content.bundle_id() != ref.bundle_id:
        raise BundleIntegrityError(
            f"bundle {ref.short_id}: the stored content hashes to "
            f"{ref.content.bundle_id()[:12]}; a runtime loads only intact bundles"
        )
    return ref.content


def bundle_from_board_run(
    cfg: AppConfig, engine: Engine, run_id: str, strategy: str
) -> StrategyBundle:
    """The bundle of rule baseline `strategy` of baseline board run `run_id`, with the configured
    risk profile and cost model.

    Raises:
        RegistryStateError: for another kind of run, an unknown strategy, or a forecast-sign
            strategy (it needs registered model versions, ML-009).
    """
    run = registry.get_run(engine, run_id)
    if run.kind != BOARD_KIND or run.dataset_id is None:
        raise RegistryStateError(f"run {run_id} is not a baseline board run with a dataset")
    board = BoardConfig.model_validate(run.config["run"]["board"])
    rules = board.strategies()
    if strategy not in rules:
        raise RegistryStateError(
            f"{strategy!r} is not a rule of board run {run_id}; a forecast-sign strategy needs "
            "registered model versions before it can be bundled (ML-009)"
        )
    rule = rules[strategy]
    spec = recorded_spec(engine, run.dataset_id)
    config: dict[str, Any] = {"rule": rule.model_dump(mode="json")}
    if rule.vol_target and board.vol_target is not None:
        config["vol_target"] = board.vol_target.model_dump(mode="json")
    costs = CostModel.from_config(cfg, spec.instrument)
    return StrategyBundle(
        instrument=spec.instrument,
        source=spec.source,
        base_timeframe=spec.base_timeframe.value,
        price_basis=spec.price_basis.value,
        feature_set_version=f"{spec.feature_set.name}.{spec.feature_set.version}",
        strategy=BundleStrategy(
            kind="baseline_rule",
            name=strategy,
            config=config,
            signal_timeframe=board.signal_timeframe,
        ),
        risk_config=cfg.risk_config().model_dump(mode="json"),
        cost_model_version=cost_model_version(costs),
    )


def _bundle(r: StrategyBundleRecord) -> BundleRef:
    return BundleRef(
        bundle_id=r.bundle_id,
        name=r.name,
        content=StrategyBundle.model_validate(r.content_json),
        origin_run_id=r.origin_run_id,
        origin_strategy=r.origin_strategy,
        status=Status(r.status),
        created_at=r.created_at,
    )
