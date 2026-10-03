"""Gate records and the status transitions they allow (MREG-002).

**Gate results.** `record_gate_result` stores one evaluation of a gate (R1 ... R4 of
``config/gates.yaml``) on a subject: every criterion of the gate with its measured value and
outcome, the criteria that could not be evaluated, those that do not apply, the evaluator, the
evidence files and the hash of the evidence policy it was judged under. Whether it **passed** is
computed here, never supplied:

- every criterion of the gate must appear, as a check, as not evaluated or as not applicable; a
  criterion that appears nowhere is recorded as missing;
- a criterion may be not applicable only by one of the owner's rules (``NOT_APPLICABLE``: PBO
  without a meaningful selection and the neighbourhood of parameters fixed a priori, C-25);
- it passes only when every check passes and nothing is not evaluated or missing.

Results are append-only; a re-evaluation adds a row, and a promotion reads the **latest** result
of its gate, so a later failure withdraws an earlier pass.

**Transitions** (`promote`, `retire`). A status moves one step along the promotion order
(`xq.registry.models.PROMOTIONS`) and needs a passing latest result of the matching gate:

| To | Gate |
| --- | --- |
| candidate | R1 research candidate |
| validated | R2 validated |
| vault_passed | R3 vault pass |
| paper | R3 (the gate that moves a candidate to paper trading) |
| live_eligible | R4 paper pass |

``live`` needs the GATE-004 human review, which is not built: it is refused. Retiring needs no
gate. Every change is appended to the status history with the gate result that allowed it. The
database's triggers enforce the same steps and gates (migration 0012), so a direct edit of a
status fails too (ADR 0060).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from xq.core.config import GateCheck, GatesConfig, gates_hash
from xq.core.time import utc_now
from xq.registry.models import (
    PROMOTIONS,
    RegistryStateError,
    Status,
    StatusChange,
    SubjectKind,
)
from xq.tracking.db import session_factory
from xq.tracking.models import GateResultRecord, ModelVersionRecord, StatusHistoryRecord

GATES = ("R1", "R2", "R3", "R4")
#: The gate a promotion to each status needs (module docstring).
GATE_FOR: Mapping[Status, str] = {
    Status.CANDIDATE: "R1",
    Status.VALIDATED: "R2",
    Status.VAULT_PASSED: "R3",
    Status.PAPER: "R3",
    Status.LIVE_ELIGIBLE: "R4",
}
#: Criteria that may be not applicable, by the owner's rules only (C-25, ADR 0058).
NOT_APPLICABLE: Mapping[str, frozenset[str]] = {
    "R2": frozenset({"pbo_max", "parameter_neighbourhood.profitable_share_min"}),
}
#: Subject kind -> (table model, id column); strategy bundles are added by MREG-003.
SUBJECTS: dict[SubjectKind, tuple[Any, str]] = {
    SubjectKind.MODEL_VERSION: (ModelVersionRecord, "model_version_id"),
}


@dataclass(frozen=True)
class GateResultRef:
    """A recorded gate result (module docstring)."""

    gate_result_id: int
    subject_kind: SubjectKind
    subject_id: str
    gate: str
    criteria: list[dict[str, Any]]
    values: dict[str, Any]
    passed: bool
    evaluator: str
    evidence_paths: list[str]
    gates_hash: str
    run_id: str | None
    created_at: pd.Timestamp

    def describe(self) -> str:
        """One line: gate, outcome, and what failed or was not evaluated."""
        outcome = "PASS" if self.passed else "FAIL"
        failed = [k for k, v in self.values["checks"].items() if not v["passed"]]
        parts = [f"{self.gate} {outcome} (result {self.gate_result_id})"]
        if failed:
            parts.append(f"failed: {', '.join(failed)}")
        if self.values["not_evaluated"]:
            parts.append(f"not evaluated: {', '.join(self.values['not_evaluated'])}")
        if self.values["missing"]:
            parts.append(f"missing: {', '.join(self.values['missing'])}")
        if self.values["not_applicable"]:
            parts.append(f"not applicable: {', '.join(self.values['not_applicable'])}")
        return "; ".join(parts)


def record_gate_result(
    engine: Engine,
    gates: GatesConfig,
    *,
    subject_kind: SubjectKind,
    subject_id: str,
    gate: str,
    checks: Sequence[GateCheck],
    not_evaluated: Mapping[str, str],
    not_applicable: Mapping[str, str],
    evaluator: str,
    evidence_paths: Sequence[str],
    run_id: str | None,
) -> GateResultRef:
    """Record one evaluation of `gate` on a subject; whether it passed is computed here.

    Raises:
        RegistryStateError: for an unknown subject.
        ValueError: for an unknown gate, a check of another gate or an unknown criterion, a
            criterion listed twice, or a not-applicable criterion no owner's rule allows.
    """
    if gate not in GATES:
        raise ValueError(f"unknown gate {gate!r}; gates are {list(GATES)}")
    criteria = [c for c in gates.criteria() if c.gate == gate]
    keys = [c.key for c in criteria]
    checked = {c.criterion.key: c for c in checks}
    for check in checks:
        if check.criterion.gate != gate or check.criterion.key not in keys:
            raise ValueError(f"{check.criterion.gate} {check.criterion.key} is not a {gate} check")
    listed = [*checked, *not_evaluated, *not_applicable]
    if len(listed) != len(set(listed)):
        raise ValueError(f"a {gate} criterion is listed twice: {sorted(listed)}")
    unknown = [k for k in (*not_evaluated, *not_applicable) if k not in keys]
    if unknown:
        raise ValueError(f"{unknown} are not criteria of {gate}")
    allowed = NOT_APPLICABLE.get(gate, frozenset())
    refused = [k for k in not_applicable if k not in allowed]
    if refused:
        raise ValueError(
            f"{gate} {refused} may not be not applicable: only the owner's rules make a "
            f"criterion not applicable ({sorted(allowed) or 'none for this gate'})"
        )
    missing = [k for k in keys if k not in listed]
    passed = (
        len(checked) > 0 and all(c.passed for c in checks) and not not_evaluated and not missing
    )
    values = {
        "checks": {
            key: {"value": _number(c.value), "passed": c.passed, "warnings": list(c.warnings)}
            for key, c in checked.items()
        },
        "not_evaluated": dict(not_evaluated),
        "not_applicable": dict(not_applicable),
        "missing": missing,
    }
    with session_factory(engine)() as session:
        _subject(session, subject_kind, subject_id)
        record = GateResultRecord(
            subject_kind=subject_kind.value,
            subject_id=subject_id,
            gate=gate,
            criteria_json=[
                {"key": c.key, "measure": c.measure, "op": c.op, "threshold": c.threshold}
                for c in criteria
            ],
            values_json=values,
            passed=passed,
            evaluator=evaluator,
            evidence_paths=list(evidence_paths),
            gates_hash=gates_hash(gates),
            run_id=run_id,
            created_at=utc_now(),
        )
        session.add(record)
        session.commit()
        return _result(record)


def latest_gate_result(
    engine: Engine, kind: SubjectKind, subject_id: str, gate: str
) -> GateResultRef | None:
    """The latest recorded result of `gate` on a subject, or None."""
    with session_factory(engine)() as session:
        record = session.scalars(
            select(GateResultRecord)
            .where(
                GateResultRecord.subject_kind == kind.value,
                GateResultRecord.subject_id == subject_id,
                GateResultRecord.gate == gate,
            )
            .order_by(GateResultRecord.gate_result_id.desc())
        ).first()
        return None if record is None else _result(record)


def list_gate_results(engine: Engine, kind: SubjectKind, subject_id: str) -> list[GateResultRef]:
    """Every gate result of a subject, oldest first."""
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(GateResultRecord)
            .where(
                GateResultRecord.subject_kind == kind.value,
                GateResultRecord.subject_id == subject_id,
            )
            .order_by(GateResultRecord.gate_result_id)
        ).all()
        return [_result(r) for r in rows]


def current_status(engine: Engine, kind: SubjectKind, subject_id: str) -> Status:
    """A subject's status.

    Raises:
        RegistryStateError: for an unknown subject.
    """
    with session_factory(engine)() as session:
        return Status(_subject(session, kind, subject_id).status)


def promote(
    engine: Engine, kind: SubjectKind, subject_id: str, to: Status, *, actor: str, reason: str
) -> StatusChange:
    """Move a subject one step up the promotion order (module docstring).

    Raises:
        RegistryStateError: for an unknown subject, a step out of order, ``live`` (GATE-004), or
            no passing latest result of the matching gate.
    """
    if to is Status.LIVE:
        raise RegistryStateError(
            "a live status transition needs the GATE-004 human review, which is not built"
        )
    if to is Status.RETIRED:
        raise RegistryStateError("retiring is not a promotion; use retire")
    status = current_status(engine, kind, subject_id)
    if PROMOTIONS.get(status) is not to:
        raise RegistryStateError(
            f"{kind} {subject_id} is {status}; the next status is "
            f"{PROMOTIONS.get(status, 'none')}, not {to}"
        )
    gate = GATE_FOR[to]
    result = latest_gate_result(engine, kind, subject_id, gate)
    if result is None or not result.passed:
        latest = "none" if result is None else result.describe()
        raise RegistryStateError(
            f"promotion of {kind} {subject_id} to {to} needs a passing {gate} gate result; "
            f"latest: {latest}"
        )
    return _change(engine, kind, subject_id, status, to, result.gate_result_id, actor, reason)


def retire(
    engine: Engine, kind: SubjectKind, subject_id: str, *, actor: str, reason: str
) -> StatusChange:
    """Retire a subject from any status but retired; no gate is needed.

    Raises:
        RegistryStateError: for an unknown or already retired subject.
    """
    status = current_status(engine, kind, subject_id)
    if status is Status.RETIRED:
        raise RegistryStateError(f"{kind} {subject_id} is already retired")
    return _change(engine, kind, subject_id, status, Status.RETIRED, None, actor, reason)


def _change(
    engine: Engine,
    kind: SubjectKind,
    subject_id: str,
    before: Status,
    after: Status,
    gate_result_id: int | None,
    actor: str,
    reason: str,
) -> StatusChange:
    now = utc_now()
    with session_factory(engine)() as session:
        record = _subject(session, kind, subject_id)
        if record.status != before.value:
            raise RegistryStateError(f"{kind} {subject_id} changed status concurrently")
        record.status = after.value
        session.add(
            StatusHistoryRecord(
                subject_kind=kind.value,
                subject_id=subject_id,
                from_status=before.value,
                to_status=after.value,
                gate_result_id=gate_result_id,
                actor=actor,
                reason=reason,
                changed_at=now,
            )
        )
        try:
            session.commit()
        except IntegrityError as exc:  # the database's own check (migration 0012)
            raise RegistryStateError(f"the database refused the status change: {exc.orig}") from exc
    return StatusChange(kind, subject_id, before, after, gate_result_id, actor, reason, now)


def _subject(session: Any, kind: SubjectKind, subject_id: str) -> Any:
    model, _ = SUBJECTS[kind]
    record = session.get(model, subject_id)
    if record is None:
        raise RegistryStateError(f"{kind} {subject_id} is not registered")
    return record


def _number(value: float) -> float | None:
    number = float(value)
    return number if math.isfinite(number) else None


def _result(r: GateResultRecord) -> GateResultRef:
    return GateResultRef(
        gate_result_id=r.gate_result_id,
        subject_kind=SubjectKind(r.subject_kind),
        subject_id=r.subject_id,
        gate=r.gate,
        criteria=list(r.criteria_json),
        values=dict(r.values_json),
        passed=r.passed,
        evaluator=r.evaluator,
        evidence_paths=list(r.evidence_paths),
        gates_hash=r.gates_hash,
        run_id=r.run_id,
        created_at=r.created_at,
    )
