"""MREG-003: content-hashed strategy bundles. The same inputs give the same id, another input
another bundle; a bundle is immutable and never deleted; it is promoted only through the gates;
a runtime loads it only if its stored content still hashes to its id (ADR 0060)."""

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
    BundleIntegrityError,
    BundleStrategy,
    StrategyBundle,
    get_bundle,
    list_bundles,
    load_bundle,
    register_bundle,
    resolve_bundle_id,
)
from xq.registry.gates import promote, record_gate_result
from xq.registry.models import RegistryStateError, Status, SubjectKind, status_history
from xq.tracking.db import create_db_engine, upgrade_to_head

CFG = repo_config()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(config(tmp_path).database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


def bundle(**changes: object) -> StrategyBundle:
    content: dict[str, object] = {
        "instrument": "xauusd",
        "source": "mt5_primary",
        "base_timeframe": "15m",
        "price_basis": "mid",
        "feature_set_version": "base.v1",
        "strategy": BundleStrategy(
            kind="baseline_rule",
            name="tsmom_252",
            config={"rule": {"rule": "time_series_momentum", "params": {"lookback": 252}}},
            signal_timeframe="1d",
        ),
        "risk_config": CFG.risk_config().model_dump(mode="json"),
        "cost_model_version": "placeholder@0123456789ab",
    }
    content.update(changes)
    return StrategyBundle.model_validate(content)


def test_the_same_inputs_give_the_same_id() -> None:
    first, again = bundle(), bundle()
    assert first.bundle_id() == again.bundle_id()
    assert len(first.bundle_id()) == 64
    reordered = StrategyBundle.model_validate(
        dict(reversed(list(first.model_dump(mode="json").items())))
    )
    assert reordered.bundle_id() == first.bundle_id()  # canonical JSON: key order is irrelevant
    risk = dict(first.risk_config)
    risk["sizing"] = {**risk["sizing"], "risk_per_trade": 0.004}
    changed = [
        bundle(risk_config=risk),
        bundle(cost_model_version="placeholder@ffffffffffff"),
        bundle(feature_set_version="base.v2"),
        bundle(
            strategy=BundleStrategy(
                kind="baseline_rule",
                name="tsmom_252",
                config={"rule": {"rule": "time_series_momentum", "params": {"lookback": 253}}},
                signal_timeframe="1d",
            )
        ),
    ]
    ids = {c.bundle_id() for c in changed}
    assert len(ids) == len(changed)
    assert first.bundle_id() not in ids


def test_registration_is_idempotent_and_the_content_immutable(engine: Engine) -> None:
    ref = register_bundle(
        engine, bundle(), name="tsmom", origin_run_id=None, origin_strategy=None, actor="me"
    )
    again = register_bundle(
        engine, bundle(), name="other", origin_run_id=None, origin_strategy=None, actor="me"
    )
    assert again == ref  # the content decides; the first registration stands
    assert ref.status is Status.DRAFT
    assert len(status_history(engine, SubjectKind.BUNDLE, ref.bundle_id)) == 1
    assert get_bundle(engine, ref.short_id) == ref
    assert resolve_bundle_id(engine, ref.bundle_id[:8]) == ref.bundle_id
    with pytest.raises(RegistryStateError, match="at least 8"):
        resolve_bundle_id(engine, "abc")
    assert list_bundles(engine) == [ref]
    assert load_bundle(engine, ref.bundle_id) == bundle()
    where = {"id": ref.bundle_id}
    for statement, message in [
        ("UPDATE strategy_bundles SET content_json = '{}' WHERE bundle_id = :id", "immutable"),
        ("UPDATE strategy_bundles SET name = 'x' WHERE bundle_id = :id", "immutable"),
        ("DELETE FROM strategy_bundles WHERE bundle_id = :id", "never deleted"),
        (
            "UPDATE strategy_bundles SET status = 'candidate' WHERE bundle_id = :id",
            "needs a passing gate result",
        ),
        ("UPDATE strategy_bundles SET status = 'paper' WHERE bundle_id = :id", "not allowed"),
    ]:
        with pytest.raises(IntegrityError, match=message), engine.begin() as connection:
            connection.execute(text(statement), where)


def test_a_bundle_is_promoted_only_through_its_gates(engine: Engine) -> None:
    ref = register_bundle(
        engine, bundle(), name="tsmom", origin_run_id=None, origin_strategy=None, actor="me"
    )
    with pytest.raises(RegistryStateError, match="needs a passing R1 gate result"):
        promote(engine, SubjectKind.BUNDLE, ref.bundle_id, Status.CANDIDATE, actor="me", reason="")
    gates = CFG.gates_config()
    r1 = [
        c.check(c.threshold + (0.001 if c.op in (">", ">=") else -0.001))
        for c in gates.criteria()
        if c.gate == "R1"
    ]
    result = record_gate_result(
        engine,
        gates,
        subject_kind=SubjectKind.BUNDLE,
        subject_id=ref.bundle_id,
        gate="R1",
        checks=r1,
        not_evaluated={},
        not_applicable={},
        evaluator="test",
        evidence_paths=[],
        run_id=None,
    )
    change = promote(
        engine, SubjectKind.BUNDLE, ref.bundle_id, Status.CANDIDATE, actor="me", reason="R1"
    )
    assert change.gate_result_id == result.gate_result_id
    assert get_bundle(engine, ref.bundle_id).status is Status.CANDIDATE


def test_a_tampered_bundle_is_never_loaded(engine: Engine) -> None:
    ref = register_bundle(
        engine, bundle(), name="tsmom", origin_run_id=None, origin_strategy=None, actor="me"
    )
    risky = bundle(cost_model_version="placeholder@ffffffffffff").model_dump_json()
    with engine.begin() as connection:  # an administrator bypassing the database's protection
        connection.execute(text("DROP TRIGGER trg_strategy_bundles_immutable"))
        connection.execute(
            text("UPDATE strategy_bundles SET content_json = :c WHERE bundle_id = :id"),
            {"c": risky, "id": ref.bundle_id},
        )
    with pytest.raises(BundleIntegrityError, match="only intact bundles"):
        load_bundle(engine, ref.bundle_id)


def test_the_registry_cli_shows_and_refuses_ungated_promotions(
    engine: Engine, tmp_path: Path
) -> None:
    ref = register_bundle(
        engine, bundle(), name="tsmom", origin_run_id=None, origin_strategy=None, actor="me"
    )

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

    code, output = xq("registry", "list")
    assert code == 0, output
    assert output.startswith(f"{ref.short_id}\tdraft\ttsmom")
    code, output = xq("registry", "show", ref.bundle_id[:10])
    assert code == 0, output
    assert '"cost_model_version": "placeholder@0123456789ab"' in output
    assert "status: - -> draft by me (registered)" in output
    code, output = xq(
        "registry", "promote", ref.short_id, "--to", "candidate", "--actor", "me", "--reason", "x"
    )
    assert code == 2
    assert "needs a passing R1 gate result; latest: none" in output
    code, output = xq(
        "registry", "promote", ref.short_id, "--to", "famous", "--actor", "me", "--reason", "x"
    )
    assert code == 2
    assert "unknown status 'famous'" in output
    code, output = xq("registry", "retire", ref.short_id, "--actor", "me", "--reason", "unused")
    assert code == 0, output
    assert output.strip() == f"bundle {ref.short_id}: draft -> retired"
