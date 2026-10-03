"""MREG-002: gate records and enforced transitions. Promotion without a passing gate record fails,
in the service and in the database; the latest result of a gate decides; whether a result passed
is computed from its checks, never supplied; the service and the database's trigger agree on every
status change (ADR 0060)."""

import itertools
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from helpers.pipeline import REPO, config
from helpers.quality import repo_config
from xq.core.config import GateCheck
from xq.registry.gates import (
    GATE_FOR,
    latest_gate_result,
    list_gate_results,
    promote,
    record_gate_result,
    retire,
)
from xq.registry.models import (
    PROMOTIONS,
    RegistryStateError,
    Status,
    SubjectKind,
    add_model_version,
    get_model_version,
    register_model,
    status_history,
)
from xq.tracking.db import create_db_engine, upgrade_to_head

GATES = repo_config().gates_config()
KIND = SubjectKind.MODEL_VERSION


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(config(tmp_path).database_url())
    upgrade_to_head(engine, REPO / "migrations")
    register_model(engine, "logit_1h", task="classification", description="test")
    yield engine
    engine.dispose()


def new_version(engine: Engine, tmp_path: Path) -> str:
    artifact = tmp_path / f"model-{len(list(tmp_path.iterdir()))}.bin"
    artifact.write_bytes(artifact.name.encode())
    ref = add_model_version(
        engine,
        "logit_1h",
        artifact=artifact,
        dataset_id=None,
        feature_set_version="base.v1",
        target="fwd_ret_mid_1h",
        train_window={},
        hyperparams={},
        metrics_snapshot={},
        git_sha="abc",
        run_id=None,
        actor="test",
    )
    return ref.model_version_id


def checks(gate: str, *, passing: bool = True) -> list[GateCheck]:
    """A check of every criterion of `gate`, all passing or the first failing."""
    out = []
    for n, criterion in enumerate(c for c in GATES.criteria() if c.gate == gate):
        good = criterion.threshold + (1 if criterion.op in (">", ">=") else -1) * 0.001
        bad = criterion.threshold - (1 if criterion.op in (">", ">=") else -1) * 0.001
        out.append(criterion.check(good if passing or n else bad))
    return out


def record(engine: Engine, subject: str, gate: str, *, passing: bool = True) -> int:
    result = record_gate_result(
        engine,
        GATES,
        subject_kind=KIND,
        subject_id=subject,
        gate=gate,
        checks=checks(gate, passing=passing),
        not_evaluated={},
        not_applicable={},
        evaluator="test",
        evidence_paths=["reports/x.md"],
        run_id=None,
    )
    assert result.passed is passing
    return result.gate_result_id


def test_promotion_without_a_passing_gate_record_fails(engine: Engine, tmp_path: Path) -> None:
    subject = new_version(engine, tmp_path)
    with pytest.raises(RegistryStateError, match="needs a passing R1 gate result; latest: none"):
        promote(engine, KIND, subject, Status.CANDIDATE, actor="me", reason="go")
    record(engine, subject, "R1", passing=False)
    with pytest.raises(RegistryStateError, match="latest: R1 FAIL"):
        promote(engine, KIND, subject, Status.CANDIDATE, actor="me", reason="go")
    passing = record(engine, subject, "R1")
    change = promote(engine, KIND, subject, Status.CANDIDATE, actor="me", reason="R1 passed")
    assert (change.from_status, change.to_status, change.gate_result_id) == (
        Status.DRAFT,
        Status.CANDIDATE,
        passing,
    )
    assert get_model_version(engine, subject).status is Status.CANDIDATE
    history = status_history(engine, KIND, subject)
    assert [h.to_status for h in history] == [Status.DRAFT, Status.CANDIDATE]
    assert history[-1].gate_result_id == passing
    # the latest result decides: a later failure withdraws the pass
    record(engine, subject, "R2")
    record(engine, subject, "R2", passing=False)
    with pytest.raises(RegistryStateError, match="latest: R2 FAIL"):
        promote(engine, KIND, subject, Status.VALIDATED, actor="me", reason="go")


def test_steps_are_taken_in_order_and_live_is_not_reachable(engine: Engine, tmp_path: Path) -> None:
    subject = new_version(engine, tmp_path)
    for gate in ("R1", "R2", "R3", "R4"):
        record(engine, subject, gate)
    with pytest.raises(RegistryStateError, match="the next status is candidate, not validated"):
        promote(engine, KIND, subject, Status.VALIDATED, actor="me", reason="skip")
    for status in (
        Status.CANDIDATE,
        Status.VALIDATED,
        Status.VAULT_PASSED,
        Status.PAPER,
        Status.LIVE_ELIGIBLE,
    ):
        promote(engine, KIND, subject, status, actor="me", reason="gates passed")
    with pytest.raises(RegistryStateError, match="GATE-004"):
        promote(engine, KIND, subject, Status.LIVE, actor="me", reason="go live")
    retire(engine, KIND, subject, actor="me", reason="done")
    with pytest.raises(RegistryStateError, match="already retired"):
        retire(engine, KIND, subject, actor="me", reason="again")
    with pytest.raises(RegistryStateError, match="is retired"):
        promote(engine, KIND, subject, Status.CANDIDATE, actor="me", reason="revive")
    assert [h.to_status for h in status_history(engine, KIND, subject)][-1] is Status.RETIRED


def test_the_database_enforces_the_same_rules_as_the_service(
    engine: Engine, tmp_path: Path
) -> None:
    """Every (from, to) pair: a direct UPDATE succeeds exactly when the service's rules allow it,
    with and without a passing gate result."""
    statuses = list(Status)
    for before, after in itertools.permutations(statuses, 2):
        for gated in (False, True):
            subject = new_version(engine, tmp_path)
            with engine.begin() as connection:  # place the subject at `before`, bypassing gates
                connection.execute(text("DROP TRIGGER trg_model_versions_status"))
                connection.execute(
                    text("UPDATE model_versions SET status = :s WHERE model_version_id = :id"),
                    {"s": before.value, "id": subject},
                )
            upgrade_trigger(engine)
            if gated and after in GATE_FOR:
                record(engine, subject, GATE_FOR[after])
            allowed = (after is Status.RETIRED and before is not Status.RETIRED) or (
                PROMOTIONS.get(before) is after and after in GATE_FOR and gated
            )
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text("UPDATE model_versions SET status = :s WHERE model_version_id = :id"),
                        {"s": after.value, "id": subject},
                    )
                done = True
            except IntegrityError:
                done = False
            assert done is allowed, (before, after, gated)


def upgrade_trigger(engine: Engine) -> None:
    """Recreate the status trigger exactly as migration 0012 defines it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migration_0012", REPO / "migrations" / "versions" / "0012_gate_results.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    body = module.TRIGGERS["trg_model_versions_status"]
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TRIGGER trg_model_versions_status {body}"))


def test_whether_a_result_passed_is_computed_and_complete(engine: Engine, tmp_path: Path) -> None:
    subject = new_version(engine, tmp_path)
    r2 = checks("R2")

    def result(**changes):  # type: ignore[no-untyped-def]
        arguments = {
            "subject_kind": KIND,
            "subject_id": subject,
            "gate": "R2",
            "checks": r2,
            "not_evaluated": {},
            "not_applicable": {},
            "evaluator": "test",
            "evidence_paths": [],
            "run_id": None,
            **changes,
        }
        return record_gate_result(engine, GATES, **arguments)

    assert result().passed
    missing = result(checks=r2[1:])
    assert not missing.passed
    assert missing.values["missing"] == [r2[0].criterion.key]
    pbo = next(c for c in r2 if c.criterion.key == "pbo_max")
    others = [c for c in r2 if c is not pbo]
    assert result(checks=others, not_applicable={"pbo_max": "no meaningful selection"}).passed
    assert not result(checks=others, not_evaluated={"pbo_max": "too few days"}).passed
    dsr = next(c for c in r2 if c.criterion.key == "dsr_min")
    with pytest.raises(ValueError, match="only the owner's rules"):
        result(checks=[c for c in r2 if c is not dsr], not_applicable={"dsr_min": "no"})
    with pytest.raises(ValueError, match="is not a R2 check"):
        result(checks=[*r2, *checks("R1")])
    with pytest.raises(ValueError, match="listed twice"):
        result(not_evaluated={"pbo_max": "x"})
    with pytest.raises(ValueError, match="unknown gate"):
        result(gate="R9")
    with pytest.raises(RegistryStateError, match="not registered"):
        result(subject_id="01UNKNOWN0000000000000000")
    results = list_gate_results(engine, KIND, subject)
    assert [r.passed for r in results] == [True, False, True, False]
    assert latest_gate_result(engine, KIND, subject, "R2") == results[-1]
    assert all(r.gates_hash and r.criteria for r in results)
    with pytest.raises(IntegrityError, match="append-only"), engine.begin() as connection:
        connection.execute(text("UPDATE gate_results SET passed = 1"))
    with pytest.raises(IntegrityError, match="append-only"), engine.begin() as connection:
        connection.execute(text("DELETE FROM gate_results"))
