"""Walk-forward fold results (WF-002).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fold_results",
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("evaluation", sa.String(length=128), nullable=False),
        sa.Column("fold_id", sa.String(length=64), nullable=False),
        sa.Column("train_start", sa.BigInteger(), nullable=False),
        sa.Column("train_end", sa.BigInteger(), nullable=False),
        sa.Column("test_start", sa.BigInteger(), nullable=False),
        sa.Column("test_end", sa.BigInteger(), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_fold_results_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("run_id", "evaluation", "fold_id", name=op.f("pk_fold_results")),
    )


def downgrade() -> None:
    op.drop_table("fold_results")
