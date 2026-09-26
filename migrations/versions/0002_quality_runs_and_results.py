"""Quality runs and graded check results (DQ-006).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_runs",
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("start_utc", sa.BigInteger(), nullable=False),
        sa.Column("end_utc", sa.BigInteger(), nullable=False),
        sa.Column("rules_version", sa.String(length=32), nullable=False),
        sa.Column("build_version", sa.String(length=32), nullable=False),
        sa.Column("includes_vault", sa.Boolean(), nullable=False),
        sa.Column("git_sha", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("report_path", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_quality_runs_source_id_data_sources"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_quality_runs")),
    )
    op.create_table(
        "quality_results",
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("partition_id", sa.String(length=96), nullable=False),
        sa.Column("check_id", sa.String(length=64), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=False),
        sa.Column("warn_threshold", sa.Float(), nullable=True),
        sa.Column("fail_threshold", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["quality_runs.run_id"], name=op.f("fk_quality_results_run_id_quality_runs")
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "partition_id", "check_id", name=op.f("pk_quality_results")
        ),
    )


def downgrade() -> None:
    op.drop_table("quality_results")
    op.drop_table("quality_runs")
