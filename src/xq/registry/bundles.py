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

**The active bundle of an environment** (MREG-005). `activate` points an environment (``paper``,
``prod``) at a bundle whose status allows it (paper: paper, live_eligible or live; prod: live);
`rollback` restores the bundle that was active before the current one, exactly (the same id), and
repeated rollbacks walk further back. Every change is a row of an append-only history.
`load_active_bundle` gives a runtime the active bundle's content, checked against its id, and
refuses one whose status no longer allows the environment (retired since, for example). A runtime
switches to a newly active bundle only when it is flat or at the next bar (`may_switch`), never in
the middle of handling one.

**Performance history** (MREG-004). `append_performance` appends one row per trading day and
source (``backtest``, ``vault``, ``paper``, ``live``): the net return on the capital, the net P&L
and the trades closed, with the run that produced it. Days are appended in order and never
rewritten. Registering a bundle from a board run appends its out-of-sample screen as its
``backtest`` history (`append_board_history`); the vault evaluation appends ``vault`` rows
(GATE-002), paper and live trading their own later (PAPER-004, PAPER-006).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, select

from xq.backtest.costs import CostModel
from xq.backtest.report import cost_model_version
from xq.core.config import AppConfig
from xq.core.time import utc_now
from xq.data.raw_store import sha256_file
from xq.datasets.builder import recorded_spec
from xq.models.board import BoardConfig
from xq.registry.models import RegistryStateError, Status, SubjectKind
from xq.tracking import registry
from xq.tracking.db import session_factory
from xq.tracking.models import (
    ActiveBundleRecord,
    BundlePerformanceRecord,
    StatusHistoryRecord,
    StrategyBundleRecord,
)

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


#: Environment -> the statuses a bundle needs to be active there (migration 0014 enforces it).
ENVIRONMENTS: dict[str, frozenset[Status]] = {
    "paper": frozenset({Status.PAPER, Status.LIVE_ELIGIBLE, Status.LIVE}),
    "prod": frozenset({Status.LIVE}),
}


@dataclass(frozen=True)
class Activation:
    """One change of an environment's active bundle."""

    environment: str
    bundle_id: str
    previous_bundle_id: str | None
    action: Literal["activate", "rollback"]
    actor: str
    reason: str
    activated_at: pd.Timestamp


def activate(
    engine: Engine, environment: str, bundle_id: str, *, actor: str, reason: str
) -> Activation:
    """Make `bundle_id` the active bundle of `environment` (module docstring).

    Raises:
        RegistryStateError: for an unknown environment or bundle, a bundle whose status does not
            allow the environment, or the bundle already active there.
    """
    ref = get_bundle(engine, bundle_id)
    _check_allowed(environment, ref)
    current = active_bundle(engine, environment)
    if current == ref.bundle_id:
        raise RegistryStateError(f"bundle {ref.short_id} is already active in {environment}")
    return _append(engine, environment, ref.bundle_id, current, "activate", actor, reason)


def rollback(engine: Engine, environment: str, *, actor: str, reason: str) -> Activation:
    """Restore the bundle that was active in `environment` before the current one.

    Raises:
        RegistryStateError: for an unknown environment, nothing to roll back to, or a previous
            bundle whose status no longer allows the environment.
    """
    history = activation_history(engine, environment)
    if not history or history[-1].previous_bundle_id is None:
        raise RegistryStateError(f"{environment} has no previous bundle to roll back to")
    target = history[-1].previous_bundle_id
    _check_allowed(environment, get_bundle(engine, target))
    # the bundle active before the target became active, so a further rollback walks back
    before = next(
        (a.previous_bundle_id for a in reversed(history[:-1]) if a.bundle_id == target), None
    )
    return _append(engine, environment, target, before, "rollback", actor, reason)


def active_bundle(engine: Engine, environment: str) -> str | None:
    """The id of `environment`'s active bundle, or None.

    Raises:
        RegistryStateError: for an unknown environment.
    """
    history = activation_history(engine, environment)
    return history[-1].bundle_id if history else None


def activation_history(engine: Engine, environment: str) -> list[Activation]:
    """Every change of `environment`'s active bundle, oldest first.

    Raises:
        RegistryStateError: for an unknown environment.
    """
    _environment(environment)
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(ActiveBundleRecord)
            .where(ActiveBundleRecord.environment == environment)
            .order_by(ActiveBundleRecord.activation_id)
        ).all()
        return [
            Activation(
                r.environment,
                r.bundle_id,
                r.previous_bundle_id,
                "rollback" if r.action == "rollback" else "activate",
                r.actor,
                r.reason,
                r.activated_at,
            )
            for r in rows
        ]


def load_active_bundle(engine: Engine, environment: str) -> StrategyBundle:
    """The active bundle's content for a runtime, checked against its id and its status.

    Raises:
        RegistryStateError: for no active bundle, or one whose status no longer allows the
            environment.
        BundleIntegrityError: if its stored content no longer hashes to its id.
    """
    current = active_bundle(engine, environment)
    if current is None:
        raise RegistryStateError(f"{environment} has no active bundle")
    _check_allowed(environment, get_bundle(engine, current))
    return load_bundle(engine, current)


def may_switch(*, flat: bool, at_bar_boundary: bool) -> bool:
    """Whether a runtime may switch to a newly active bundle now: only when it is flat, or at the
    next bar boundary (module docstring)."""
    return flat or at_bar_boundary


def _environment(environment: str) -> frozenset[Status]:
    try:
        return ENVIRONMENTS[environment]
    except KeyError:
        raise RegistryStateError(
            f"unknown environment {environment!r}; environments: {sorted(ENVIRONMENTS)}"
        ) from None


def _check_allowed(environment: str, ref: BundleRef) -> None:
    allowed = _environment(environment)
    if ref.status not in allowed:
        raise RegistryStateError(
            f"bundle {ref.short_id} is {ref.status}; {environment} needs "
            f"{' or '.join(sorted(allowed))}"
        )


def _append(
    engine: Engine,
    environment: str,
    bundle_id: str,
    previous: str | None,
    action: Literal["activate", "rollback"],
    actor: str,
    reason: str,
) -> Activation:
    now = utc_now()
    with session_factory(engine)() as session:
        session.add(
            ActiveBundleRecord(
                environment=environment,
                bundle_id=bundle_id,
                previous_bundle_id=previous,
                action=action,
                actor=actor,
                reason=reason,
                activated_at=now,
            )
        )
        session.commit()
    return Activation(environment, bundle_id, previous, action, actor, reason, now)


#: Where a bundle's daily performance comes from.
SOURCES = ("backtest", "vault", "paper", "live")
RETURNS_ARTIFACT = "baseline_returns"


def append_performance(
    engine: Engine,
    bundle_id: str,
    source: str,
    daily: pd.DataFrame,
    *,
    run_id: str | None,
) -> int:
    """Append daily rows (index: trading days; ``net_return``, optional ``net_pnl`` and
    ``trades``) to a bundle's history from `source`; return the rows appended.

    Raises:
        RegistryStateError: for an unknown bundle or source, a missing or non-finite net return,
            or a day not after the last one already recorded for this source.
    """
    if source not in SOURCES:
        raise RegistryStateError(f"unknown performance source {source!r}; sources: {SOURCES}")
    ref = get_bundle(engine, bundle_id)
    if "net_return" not in daily.columns or not np.isfinite(daily["net_return"]).all():
        raise RegistryStateError("every day needs a finite net_return")
    days = [pd.Timestamp(d).date() for d in daily.index]
    if days != sorted(set(days)):
        raise RegistryStateError("days must be distinct and in order")
    with session_factory(engine)() as session:
        last = session.scalar(
            select(func.max(BundlePerformanceRecord.trading_day)).where(
                BundlePerformanceRecord.bundle_id == ref.bundle_id,
                BundlePerformanceRecord.source == source,
            )
        )
        if last is not None and days and days[0] <= last:
            raise RegistryStateError(
                f"bundle {ref.short_id} already has {source} performance up to {last}; days are "
                "appended, never rewritten"
            )
        now = utc_now()
        for day, row in zip(days, daily.to_dict(orient="records"), strict=True):
            pnl = row.get("net_pnl")
            trades = row.get("trades")
            session.add(
                BundlePerformanceRecord(
                    bundle_id=ref.bundle_id,
                    source=source,
                    trading_day=day,
                    net_return=float(row["net_return"]),
                    net_pnl=None if pnl is None or pd.isna(pnl) else float(pnl),
                    trades=None if trades is None or pd.isna(trades) else int(trades),
                    run_id=run_id,
                    recorded_at=now,
                )
            )
        session.commit()
    return len(days)


def performance_history(engine: Engine, bundle_id: str, source: str | None = None) -> pd.DataFrame:
    """A bundle's daily performance (all sources, or one), ordered by source and day."""
    ref = get_bundle(engine, bundle_id)
    with session_factory(engine)() as session:
        query = select(BundlePerformanceRecord).where(
            BundlePerformanceRecord.bundle_id == ref.bundle_id
        )
        if source is not None:
            query = query.where(BundlePerformanceRecord.source == source)
        rows = session.scalars(
            query.order_by(BundlePerformanceRecord.source, BundlePerformanceRecord.trading_day)
        ).all()
        return pd.DataFrame(
            [
                {
                    "source": r.source,
                    "trading_day": r.trading_day,
                    "net_return": r.net_return,
                    "net_pnl": r.net_pnl,
                    "trades": r.trades,
                    "run_id": r.run_id,
                }
                for r in rows
            ],
            columns=["source", "trading_day", "net_return", "net_pnl", "trades", "run_id"],
        )


def append_board_history(cfg: AppConfig, engine: Engine, bundle_id: str) -> int:
    """Append the out-of-sample screen of a bundle's origin board strategy as its ``backtest``
    history (the daily returns the run recorded, verified against their SHA-256); return the
    rows appended (0 when already recorded).

    Raises:
        RegistryStateError: for a bundle without a board origin, or an altered returns file.
    """
    ref = get_bundle(engine, bundle_id)
    if ref.origin_run_id is None or ref.origin_strategy is None:
        raise RegistryStateError(f"bundle {ref.short_id} has no board run origin")
    if len(performance_history(engine, ref.bundle_id, "backtest")):
        return 0
    artifacts = [
        a for a in registry.list_artifacts(engine, ref.origin_run_id) if a.kind == RETURNS_ARTIFACT
    ]
    if len(artifacts) != 1:
        raise RegistryStateError(f"run {ref.origin_run_id} has no single returns artifact")
    path = Path(artifacts[0].path)
    if not path.is_file() or sha256_file(path) != artifacts[0].sha256:
        raise RegistryStateError(f"{path} is missing or altered since it was recorded")
    returns = pd.read_parquet(path)[ref.origin_strategy]
    capital = cfg.backtest_config().capital_usd
    daily = pd.DataFrame(
        {"net_return": returns.to_numpy(np.float64), "net_pnl": returns.to_numpy() * capital},
        index=[date.fromisoformat(str(d)) for d in returns.index],
    )
    return append_performance(engine, ref.bundle_id, "backtest", daily, run_id=ref.origin_run_id)
