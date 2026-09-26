"""EXP-001: experiment registry schema and API (append-only CRUD)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from helpers.pipeline import REPO
from xq.tracking import registry as reg
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import Run

RUN_KW = {
    "kind": "baseline",
    "confirmatory": False,
    "git_sha": "abc",
    "config_hash": "cfg",
    "config": {"lookback": 20},
    "dataset_id": "ds-0123456789abcdef",
    "lock_hash": "lock",
    "seed": 7,
    "host": "test-host",
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


def register(engine: Engine, text: str = "statement: a\n") -> reg.HypothesisRef:
    return reg.add_hypothesis_version(
        engine, "H-0001", title="Momentum", family_id="momentum", yaml_text=text
    )


def test_hypothesis_versions(engine: Engine) -> None:
    first = register(engine)
    assert (first.version, first.status) == (1, reg.HypothesisStatus.ACTIVE)
    assert register(engine) == first  # same text: no new version
    second = register(engine, "statement: b\n")
    assert second.version == 2
    assert reg.get_hypothesis(engine, "H-0001") == second
    assert reg.get_hypothesis(engine, "H-0001", 1).status is reg.HypothesisStatus.SUPERSEDED
    assert reg.hypothesis_text(engine, "H-0001", 1) == "statement: a\n"
    assert [(h.version, h.status.value) for h in reg.list_hypotheses(engine)] == [
        (1, "superseded"),
        (2, "active"),
    ]
    with pytest.raises(reg.RegistryError, match="not registered"):
        reg.get_hypothesis(engine, "H-9999")


def test_experiments_bind_the_current_hypothesis_version(engine: Engine) -> None:
    register(engine)
    first = reg.create_experiment(engine, "H-0001", "first look")
    assert first.hypothesis_version == 1
    assert first.status is reg.ExperimentStatus.OPEN
    assert reg.open_experiment_for(engine, "H-0001", "ignored") == first
    register(engine, "statement: edited\n")
    fresh = reg.open_experiment_for(engine, "H-0001", "after the edit")
    assert fresh.experiment_id != first.experiment_id
    assert fresh.hypothesis_version == 2
    assert [e.experiment_id for e in reg.list_experiments(engine, "H-0001")] == [
        first.experiment_id,
        fresh.experiment_id,
    ]
    with pytest.raises(reg.RegistryError, match="not found"):
        reg.get_experiment(engine, "nope")


def test_run_lifecycle_metrics_and_artifacts(engine: Engine, tmp_path: Path) -> None:
    register(engine)
    experiment = reg.create_experiment(engine, "H-0001", "runs")
    run = reg.start_run(engine, experiment.experiment_id, **RUN_KW)  # type: ignore[arg-type]
    assert run.status is reg.RunStatus.RUNNING
    assert run.config == {"lookback": 20}
    reg.log_metric(engine, run.run_id, "sharpe", 0.4, fold_id="f1")
    reg.log_metric(engine, run.run_id, "sharpe", 0.1, fold_id="f2")
    artifact_file = tmp_path / "report.md"
    artifact_file.write_text("results\n")
    artifact = reg.log_artifact(engine, run.run_id, artifact_file, kind="report")
    assert len(artifact.sha256) == 64

    done = reg.finish_run(engine, run.run_id, reg.RunStatus.FINISHED)
    assert done.finished_at is not None
    assert [(m.fold_id, m.value) for m in reg.get_metrics(engine, run.run_id)] == [
        ("f1", 0.4),
        ("f2", 0.1),
    ]
    assert reg.list_artifacts(engine, run.run_id) == [artifact]
    assert reg.count_runs(engine, experiment_id=experiment.experiment_id) == 1
    assert reg.list_runs(engine, experiment.experiment_id) == [done]

    # A finished run accepts nothing more and cannot finish twice.
    with pytest.raises(reg.RegistryError, match="no longer accepts"):
        reg.log_metric(engine, run.run_id, "late", 1.0)
    with pytest.raises(reg.RegistryError, match="no longer accepts"):
        reg.finish_run(engine, run.run_id, reg.RunStatus.FAILED)
    with pytest.raises(reg.RegistryError, match="finished or failed"):
        reg.finish_run(engine, run.run_id, reg.RunStatus.RUNNING)
    with pytest.raises(reg.RegistryError, match="does not exist"):
        reg.log_artifact(engine, run.run_id, tmp_path / "missing", kind="x")


def test_runs_need_an_existing_experiment(engine: Engine) -> None:
    with pytest.raises(reg.RegistryError, match="not found"):
        reg.start_run(engine, "no-such-experiment", **RUN_KW)  # type: ignore[arg-type]
    with session_factory(engine)() as session:
        session.add(
            Run(
                run_id="01RUN000000000000000000000",
                experiment_id="missing",
                kind="x",
                confirmatory=False,
                git_sha="a",
                config_hash="b",
                config_json={},
                dataset_id=None,
                lock_hash="c",
                seed=1,
                host="h",
                started_at=reg.utc_now(),
                finished_at=None,
                status="running",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_api_has_no_deletes() -> None:
    assert not [name for name in dir(reg) if name.startswith(("delete", "remove", "drop"))]
