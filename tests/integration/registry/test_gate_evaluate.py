"""GATE-001 end to end on synthetic ticks: a baseline strategy bundle is registered with its
backtest history (MREG-004), `xq gate evaluate` validates its origin strategy, records R1 and R2
from that validation (it may fail) and writes the gate report with the plan's ten items and the
filled-in human review (GATE-003); an ungated promotion is refused; a validation of another
strategy, or an altered one, is refused. Synthetic data: an engineering check, never evidence."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from helpers.gate_board import build_world, register, xq
from xq.core.config import AppConfig
from xq.registry.bundles import get_bundle, performance_history
from xq.registry.gates import list_gate_results
from xq.registry.models import Status, SubjectKind
from xq.tracking import registry
from xq.validation.strategy import VALIDATION_KIND


@pytest.fixture(scope="module")
def world(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, AppConfig, Engine, str]]:
    root = tmp_path_factory.mktemp("gate_evaluate")
    cfg, engine, _, run_id = build_world(root, tmp_path_factory.mktemp("gate_ticks"))
    yield root, cfg, engine, run_id
    engine.dispose()


def validations(engine: Engine, run_id: str, strategy: str) -> list[registry.RunRef]:
    return [
        r
        for r in registry.list_runs(engine)
        if r.kind == VALIDATION_KIND
        and r.config["run"]["validates"] == run_id
        and r.config["run"]["strategy"] == strategy
    ]


def test_a_bundle_is_registered_with_its_backtest_history(
    world: tuple[Path, AppConfig, Engine, str],
) -> None:
    root, _, engine, run_id = world
    bundle_id = register(root, run_id, "tsmom_8@1h")
    assert register(root, run_id, "tsmom_8@1h") == bundle_id  # the same content: the same bundle
    history = performance_history(engine, bundle_id, "backtest")
    returns = registry.list_artifacts(engine, run_id)
    assert len(history) > 0
    assert set(history["run_id"]) == {run_id}
    assert any(a.kind == "baseline_returns" for a in returns)
    code, output = xq(root, "registry", "history", bundle_id[:12])
    assert code == 0, output
    assert output.startswith(f"backtest: {len(history)} trading days")
    code, output = xq(
        root, "registry", "register", "--run", run_id, "--strategy",
        "historical_mean:fwd_ret_mid_1h", "--actor", "tester",
    )  # fmt: skip
    assert code == 2
    assert "needs registered model versions" in output


def test_the_gate_evaluator_records_r1_and_r2_from_a_validation(
    world: tuple[Path, AppConfig, Engine, str],
) -> None:
    root, _, engine, run_id = world
    bundle_id = register(root, run_id, "tsmom_8@1h")
    code, output = xq(root, "gate", "evaluate", bundle_id[:12])
    assert code == 0, output  # a failed gate is a result, not an error
    (validation,) = validations(engine, run_id, "tsmom_8@1h")
    assert validation.confirmatory  # a clean tree: the evidence is citable
    results = list_gate_results(engine, SubjectKind.BUNDLE, bundle_id)
    assert [r.gate for r in results] == ["R1", "R2"]
    payload_path = next(
        Path(a.path)
        for a in registry.list_artifacts(engine, validation.run_id)
        if a.kind == "validation_json"
    )
    verdicts = json.loads(payload_path.read_text())["verdicts"]
    for result in results:
        assert result.run_id == validation.run_id
        assert result.passed is (verdicts[result.gate] == "pass")  # incomplete never passes
        assert str(payload_path) in result.evidence_paths
        assert result.describe() in output
    report = Path(output.strip().splitlines()[-1].removeprefix("report: "))
    assert report.name == "gate.md"
    document = json.loads(report.with_name("gate.json").read_text())
    assert [item["number"] for item in document["items"]] == list(range(1, 11))
    items = {item["number"]: item for item in document["items"]}
    assert items[1]["status"] == "pass"  # gated on its quality run: no FAIL partition included
    assert items[4]["status"] == "pending"  # no vault evaluation yet
    assert items[10]["status"] == "pending"  # no paper trading yet
    assert document["validation_run_id"] == validation.run_id
    assert document["reproductions"] == []
    assert (
        "Reproduction of the origin run (EXP-006, reported): not attempted." in report.read_text()
    )
    # GATE-003: the human review template, filled in next to the report, signed by nobody yet
    review = report.with_name("review.md").read_text()
    assert review.startswith("# Gate review and sign-off (GATE-003)")
    assert f"- Bundle: `{bundle_id}` (tsmom_8@1h), status at evaluation: draft" in review
    assert f"- Validation run: {validation.run_id}" in review
    assert results[0].describe() in review
    assert "{" not in review.split("## Evidence checklist")[0]  # every field filled
    assert review.count("| [ ] |") == 10  # one unticked row per release-gate item
    assert "| Reviewer | | | |" in review
    # the evaluator never promotes, and an ungated promotion is refused
    assert get_bundle(engine, bundle_id).status is Status.DRAFT
    if not results[0].passed:
        code, output = xq(
            root, "registry", "promote", bundle_id[:12], "--to", "candidate", "--actor", "me",
            "--reason", "no",
        )  # fmt: skip
        assert code == 2
        assert "needs a passing R1 gate result; latest: R1 FAIL" in output


def test_an_existing_validation_is_read_only_if_it_validates_the_bundle(
    world: tuple[Path, AppConfig, Engine, str],
) -> None:
    root, _, engine, run_id = world
    bundle_id = register(root, run_id, "tsmom_8@1h")
    other = register(root, run_id, "buy_and_hold@1h")
    (validation,) = validations(engine, run_id, "tsmom_8@1h")
    code, output = xq(root, "gate", "evaluate", other[:12], "--validation", validation.run_id)
    assert code == 2
    assert "validated 'tsmom_8@1h', not 'buy_and_hold@1h'" in output
    before = len(list_gate_results(engine, SubjectKind.BUNDLE, bundle_id))
    code, output = xq(root, "gate", "evaluate", bundle_id[:12], "--validation", validation.run_id)
    assert code == 0, output
    assert len(validations(engine, run_id, "tsmom_8@1h")) == 1  # read, not rerun
    assert len(list_gate_results(engine, SubjectKind.BUNDLE, bundle_id)) == before + 2
    path = next(
        Path(a.path)
        for a in registry.list_artifacts(engine, validation.run_id)
        if a.kind == "validation_json"
    )
    original = path.read_bytes()
    try:
        path.write_bytes(original.replace(b'"passed": false', b'"passed": true'))
        code, output = xq(
            root, "gate", "evaluate", bundle_id[:12], "--validation", validation.run_id
        )
        assert code == 2
        assert "missing or altered since it was recorded" in output
    finally:
        path.write_bytes(original)
