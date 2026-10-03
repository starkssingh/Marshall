"""MREG-001: models and model versions with statuses. A version records its artifact's SHA-256,
data, target, window, hyperparameters, metrics, code and run; it is immutable and never deleted,
its status history is append-only, and a direct database edit of its status fails: a promotion
needs a passing gate result (MREG-002), and ``live`` is never set (ADR 0060)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from helpers.pipeline import REPO, config
from xq.data.raw_store import sha256_file
from xq.registry.models import (
    RegistryStateError,
    Status,
    SubjectKind,
    add_model_version,
    get_model_version,
    list_model_versions,
    register_model,
    status_history,
)
from xq.tracking.db import create_db_engine, upgrade_to_head


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(config(tmp_path).database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


def version(engine: Engine, tmp_path: Path, name: str = "logit_1h", payload: bytes = b"m1"):  # type: ignore[no-untyped-def]
    artifact = tmp_path / f"{name}-{len(list(tmp_path.iterdir()))}.bin"
    artifact.write_bytes(payload)
    return add_model_version(
        engine,
        name,
        artifact=artifact,
        dataset_id="ds-0123456789abcdef",
        feature_set_version="base.v1",
        target="fwd_ret_mid_1h",
        train_window={"start": "2022-01-02T22:00:00Z", "end": "2024-01-01T22:00:00Z"},
        hyperparams={"C": 1.0},
        metrics_snapshot={"log_loss": 0.69},
        git_sha="abc123",
        run_id=None,
        actor="test",
    )


def test_versions_record_what_produced_them(engine: Engine, tmp_path: Path) -> None:
    model = register_model(engine, "logit_1h", task="classification", description="baseline")
    assert register_model(engine, "logit_1h", task="classification", description="x") == model
    with pytest.raises(RegistryStateError, match="task 'classification'"):
        register_model(engine, "logit_1h", task="regression", description="x")
    first = version(engine, tmp_path)
    second = version(engine, tmp_path, payload=b"m2")
    assert (first.version, second.version) == (1, 2)
    assert first.status is second.status is Status.DRAFT
    assert first.artifact_sha256 == sha256_file(Path(first.artifact_uri))
    assert first.artifact_sha256 != second.artifact_sha256
    assert first.train_window["start"] == "2022-01-02T22:00:00Z"
    assert get_model_version(engine, first.model_version_id) == first
    assert list_model_versions(engine, "logit_1h") == [first, second]
    (registered,) = status_history(engine, SubjectKind.MODEL_VERSION, first.model_version_id)
    assert (registered.from_status, registered.to_status) == (None, Status.DRAFT)
    with pytest.raises(RegistryStateError, match="not registered"):
        version(engine, tmp_path, name="unknown")
    with pytest.raises(RegistryStateError, match="does not exist"):
        add_model_version(
            engine,
            "logit_1h",
            artifact=tmp_path / "missing.bin",
            dataset_id=None,
            feature_set_version="base.v1",
            target="t",
            train_window={},
            hyperparams={},
            metrics_snapshot={},
            git_sha="abc",
            run_id=None,
            actor="test",
        )


def test_the_database_refuses_edits_deletions_and_unauthorised_promotions(
    engine: Engine, tmp_path: Path
) -> None:
    register_model(engine, "logit_1h", task="classification", description="baseline")
    ref = version(engine, tmp_path)
    where = {"id": ref.model_version_id}
    version_row = "UPDATE model_versions SET {} WHERE model_version_id = :id"
    statements = [
        (version_row.format("artifact_uri = 'x'"), "immutable"),
        (version_row.format("hyperparams_json = '{}'"), "immutable"),
        ("DELETE FROM model_versions WHERE model_version_id = :id", "never deleted"),
        ("DELETE FROM models", "never deleted"),
        (version_row.format("status = 'candidate'"), "needs a passing gate result"),
        (version_row.format("status = 'live'"), "not allowed"),
        ("UPDATE status_history SET actor = 'someone else'", "append-only"),
        ("DELETE FROM status_history", "append-only"),
    ]
    for statement, message in statements:
        with pytest.raises(IntegrityError, match=message), engine.begin() as connection:
            connection.execute(text(statement), where)
    assert get_model_version(engine, ref.model_version_id) == ref
    # retiring needs no gate, even at the database level; a retired version stays retired
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE model_versions SET status = 'retired' WHERE model_version_id = :id"), where
        )
    with pytest.raises(IntegrityError, match="not allowed"), engine.begin() as connection:
        connection.execute(
            text("UPDATE model_versions SET status = 'draft' WHERE model_version_id = :id"), where
        )
    assert get_model_version(engine, ref.model_version_id).status is Status.RETIRED
