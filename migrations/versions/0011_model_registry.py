"""Model registry: models, model versions and the status history (MREG-001).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-28

The triggers are part of the schema (ADR 0060): a model version's content never changes, nothing
is deleted, the status history is append-only, and until the gate records exist (MREG-002,
migration 0012) the only status change a model version may make is to ``retired``. They are
written for SQLite, the metadata database of the research tier; another database is refused
here rather than migrated without its enforcement (PAPER-004 adds PostgreSQL's).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_CONTENT = (
    "model_version_id, model_id, version, artifact_uri, artifact_sha256, dataset_id, "
    "feature_set_version, target, train_window_json, hyperparams_json, metrics_snapshot_json, "
    "git_sha, run_id, created_at"
)
TRIGGERS = {
    "trg_models_no_delete": "BEFORE DELETE ON models BEGIN "
    "SELECT RAISE(ABORT, 'models are never deleted'); END",
    "trg_model_versions_immutable": f"BEFORE UPDATE OF {_VERSION_CONTENT} ON model_versions BEGIN "
    "SELECT RAISE(ABORT, 'a model version is immutable; register a new version'); END",
    "trg_model_versions_no_delete": "BEFORE DELETE ON model_versions BEGIN "
    "SELECT RAISE(ABORT, 'model versions are never deleted'); END",
    "trg_model_versions_status": "BEFORE UPDATE OF status ON model_versions "
    "WHEN NEW.status <> OLD.status BEGIN "
    "SELECT RAISE(ABORT, 'status transition not allowed') "
    "WHERE NOT (OLD.status <> 'retired' AND NEW.status = 'retired'); END",
    "trg_status_history_no_update": "BEFORE UPDATE ON status_history BEGIN "
    "SELECT RAISE(ABORT, 'the status history is append-only'); END",
    "trg_status_history_no_delete": "BEFORE DELETE ON status_history BEGIN "
    "SELECT RAISE(ABORT, 'the status history is append-only'); END",
}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        raise NotImplementedError(
            f"the registry's enforcing triggers exist for SQLite only, not {dialect} (PAPER-004)"
        )
    op.create_table(
        "models",
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("task", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("model_id", name=op.f("pk_models")),
        sa.UniqueConstraint("name", name=op.f("uq_models_name")),
    )
    op.create_table(
        "model_versions",
        sa.Column("model_version_id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("dataset_id", sa.String(length=32), nullable=True),
        sa.Column("feature_set_version", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("train_window_json", sa.JSON(), nullable=False),
        sa.Column("hyperparams_json", sa.JSON(), nullable=False),
        sa.Column("metrics_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("git_sha", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_id"], ["models.model_id"], name=op.f("fk_model_versions_model_id_models")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_model_versions_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("model_version_id", name=op.f("pk_model_versions")),
        sa.UniqueConstraint("model_id", "version", name=op.f("uq_model_versions_model_id")),
    )
    op.create_table(
        "status_history",
        sa.Column("history_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("gate_result_id", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("history_id", name=op.f("pk_status_history")),
    )
    for name, body in TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("status_history")
    op.drop_table("model_versions")
    op.drop_table("models")
