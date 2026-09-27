"""Statistical tests and robustness results of validation runs (Phases 16 and 17).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stat_tests",
        sa.Column("stat_test_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("test_name", sa.String(length=160), nullable=False),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("statistic", sa.Float(), nullable=True),
        sa.Column("p_value", sa.Float(), nullable=True),
        sa.Column("adjusted_p", sa.Float(), nullable=True),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_stat_tests_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("stat_test_id", name=op.f("pk_stat_tests")),
    )
    op.create_table(
        "robustness_results",
        sa.Column("result_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("test_id", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_robustness_results_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("result_id", name=op.f("pk_robustness_results")),
    )


def downgrade() -> None:
    op.drop_table("robustness_results")
    op.drop_table("stat_tests")
