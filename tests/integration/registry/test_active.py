"""MREG-005: the active bundle of each environment. Rollback restores the exact previous bundle
hash, and repeated rollbacks walk back the history; only a bundle whose status allows the
environment can be active, in the service and in the database; the pointer's history is
append-only; a runtime switches only when flat or at the next bar (ADR 0060)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from helpers.pipeline import REPO, config
from helpers.quality import repo_config
from xq.cli.main import app
from xq.registry.bundles import (
    BundleStrategy,
    StrategyBundle,
    activate,
    activation_history,
    active_bundle,
    load_active_bundle,
    may_switch,
    register_bundle,
    rollback,
)
from xq.registry.gates import promote, record_gate_result, retire
from xq.registry.models import RegistryStateError, Status, SubjectKind
from xq.tracking.db import create_db_engine, upgrade_to_head

CFG = repo_config()
GATES = CFG.gates_config()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(config(tmp_path).database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


def bundle_at(engine: Engine, lookback: int, status: Status) -> str:
    """A registered rule bundle brought to `status` through recorded passing gate results."""
    content = StrategyBundle(
        instrument="xauusd",
        source="mt5_primary",
        base_timeframe="15m",
        price_basis="mid",
        feature_set_version="base.v1",
        strategy=BundleStrategy(
            kind="baseline_rule",
            name=f"tsmom_{lookback}",
            config={"rule": {"rule": "time_series_momentum", "params": {"lookback": lookback}}},
            signal_timeframe="1d",
        ),
        risk_config=CFG.risk_config().model_dump(mode="json"),
        cost_model_version="placeholder@0123456789ab",
    )
    ref = register_bundle(
        engine, content, name=f"tsmom_{lookback}", origin_run_id=None, origin_strategy=None,
        actor="test",
    )  # fmt: skip
    order = [Status.CANDIDATE, Status.VALIDATED, Status.VAULT_PASSED, Status.PAPER]
    for step in order[: order.index(status) + 1] if status in order else []:
        gate = {"candidate": "R1", "validated": "R2", "vault_passed": "R3", "paper": "R3"}[step]
        record_gate_result(
            engine,
            GATES,
            subject_kind=SubjectKind.BUNDLE,
            subject_id=ref.bundle_id,
            gate=gate,
            checks=[
                c.check(c.threshold + (0.001 if c.op in (">", ">=") else -0.001))
                for c in GATES.criteria()
                if c.gate == gate
            ],
            not_evaluated={},
            not_applicable={},
            evaluator="test (synthetic gate results)",
            evidence_paths=[],
            run_id=None,
        )
        promote(engine, SubjectKind.BUNDLE, ref.bundle_id, step, actor="test", reason="gates")
    return ref.bundle_id


def test_rollback_restores_the_exact_previous_bundle(engine: Engine) -> None:
    first, second, third = (bundle_at(engine, n, Status.PAPER) for n in (60, 120, 252))
    assert active_bundle(engine, "paper") is None
    for bundle_id in (first, second, third):
        activate(engine, "paper", bundle_id, actor="ops", reason="deploy")
    assert active_bundle(engine, "paper") == third
    back = rollback(engine, "paper", actor="ops", reason="incident")
    assert back.bundle_id == second  # the exact previous hash
    assert active_bundle(engine, "paper") == second
    assert rollback(engine, "paper", actor="ops", reason="again").bundle_id == first
    with pytest.raises(RegistryStateError, match="no previous bundle"):
        rollback(engine, "paper", actor="ops", reason="once more")
    activate(engine, "paper", third, actor="ops", reason="redeploy")
    assert rollback(engine, "paper", actor="ops", reason="incident").bundle_id == first
    actions = [(a.action, a.bundle_id) for a in activation_history(engine, "paper")]
    assert actions == [
        ("activate", first),
        ("activate", second),
        ("activate", third),
        ("rollback", second),
        ("rollback", first),
        ("activate", third),
        ("rollback", first),
    ]
    assert load_active_bundle(engine, "paper").strategy.name == "tsmom_60"
    with pytest.raises(RegistryStateError, match="already active"):
        activate(engine, "paper", first, actor="ops", reason="twice")
    with pytest.raises(IntegrityError, match="append-only"), engine.begin() as connection:
        connection.execute(text("UPDATE active_bundles SET actor = 'x'"))
    with pytest.raises(IntegrityError, match="append-only"), engine.begin() as connection:
        connection.execute(text("DELETE FROM active_bundles"))


def test_only_a_bundle_whose_status_allows_the_environment_is_active(engine: Engine) -> None:
    validated = bundle_at(engine, 60, Status.VALIDATED)
    paper = bundle_at(engine, 120, Status.PAPER)
    with pytest.raises(RegistryStateError, match="is validated; paper needs"):
        activate(engine, "paper", validated, actor="ops", reason="early")
    with pytest.raises(RegistryStateError, match="prod needs live"):
        activate(engine, "prod", paper, actor="ops", reason="live")
    with pytest.raises(RegistryStateError, match="unknown environment"):
        activate(engine, "staging", paper, actor="ops", reason="x")
    insert = (
        "INSERT INTO active_bundles (environment, bundle_id, previous_bundle_id, action, actor, "
        "reason, activated_at) VALUES (:env, :id, NULL, 'activate', 'x', 'x', 0)"
    )
    for env, bundle_id in (("paper", validated), ("prod", paper), ("staging", paper)):
        with pytest.raises(IntegrityError, match="does not allow"), engine.begin() as connection:
            connection.execute(text(insert), {"env": env, "id": bundle_id})
    activate(engine, "paper", paper, actor="ops", reason="deploy")
    retire(engine, SubjectKind.BUNDLE, paper, actor="ops", reason="withdrawn")
    with pytest.raises(RegistryStateError, match="is retired"):
        load_active_bundle(engine, "paper")  # a runtime never loads a retired bundle


def test_a_runtime_switches_only_when_flat_or_at_the_next_bar() -> None:
    assert may_switch(flat=True, at_bar_boundary=False)
    assert may_switch(flat=False, at_bar_boundary=True)
    assert not may_switch(flat=False, at_bar_boundary=False)


def test_the_cli_activates_and_rolls_back(engine: Engine, tmp_path: Path) -> None:
    first, second = (bundle_at(engine, n, Status.PAPER) for n in (60, 120))

    def xq(*args: str) -> tuple[int, str]:
        common = [
            "--config-dir", str(REPO / "config"),
            "--set", f"paths.root={tmp_path}",
            "--set", f"paths.migrations_dir={REPO / 'migrations'}",
            "--set", "logging.console=false",
            "--set", "logging.file=null",
        ]  # fmt: skip
        result = CliRunner().invoke(app, [*common, *args])
        return result.exit_code, result.output

    who = ("--actor", "ops", "--reason", "test")
    assert xq("registry", "active", "--env", "paper") == (0, "paper: no active bundle\n")
    for bundle_id in (first, second):
        code, output = xq("registry", "activate", bundle_id[:12], "--env", "paper", *who)
        assert code == 0, output
    code, output = xq("registry", "rollback", "--env", "paper", *who)
    assert code == 0, output
    assert output.strip() == f"paper: rolled back to {first}"
    code, output = xq("registry", "active", "--env", "paper")
    assert output.splitlines()[0] == f"paper: {first} active"
    assert len(output.splitlines()) == 4  # the pointer's whole history
    code, output = xq("registry", "activate", first[:12], "--env", "prod", *who)
    assert code == 2
    assert "prod needs live" in output
