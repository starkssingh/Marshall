"""Experiment registry: hypotheses, experiments, runs, trials, metrics, artifacts (EXP-001).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hypotheses",
        sa.Column("hypothesis_id", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("yaml_hash", sa.String(length=64), nullable=False),
        sa.Column("yaml_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("hypothesis_id", "version", name=op.f("pk_hypotheses")),
    )
    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.String(length=26), nullable=False),
        sa.Column("hypothesis_id", sa.String(length=16), nullable=False),
        sa.Column("hypothesis_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("closed_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["hypothesis_id", "hypothesis_version"],
            ["hypotheses.hypothesis_id", "hypotheses.version"],
            name="fk_experiments_hypothesis_hypotheses",
        ),
        sa.PrimaryKeyConstraint("experiment_id", name=op.f("pk_experiments")),
    )
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("experiment_id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("confirmatory", sa.Boolean(), nullable=False),
        sa.Column("git_sha", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("dataset_id", sa.String(length=32), nullable=True),
        sa.Column("lock_hash", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.experiment_id"],
            name=op.f("fk_runs_experiment_id_experiments"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_runs")),
    )
    op.create_table(
        "trials",
        sa.Column("trial_id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("evaluated_on_test", sa.Boolean(), nullable=False),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("returns_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], name=op.f("fk_trials_run_id_runs")),
        sa.PrimaryKeyConstraint("trial_id", name=op.f("pk_trials")),
    )
    op.create_table(
        "metrics",
        sa.Column("metric_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("fold_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], name=op.f("fk_metrics_run_id_runs")),
        sa.PrimaryKeyConstraint("metric_id", name=op.f("pk_metrics")),
    )
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], name=op.f("fk_artifacts_run_id_runs")),
        sa.PrimaryKeyConstraint("artifact_id", name=op.f("pk_artifacts")),
    )


def downgrade() -> None:
    for table in ("artifacts", "metrics", "trials", "runs", "experiments", "hypotheses"):
        op.drop_table(table)
