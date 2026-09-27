"""Experiment conclusions (EXP-005).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conclusions",
        sa.Column("experiment_id", sa.String(length=26), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("observed", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=False),
        sa.Column("limitations", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.experiment_id"],
            name=op.f("fk_conclusions_experiment_id_experiments"),
        ),
        sa.PrimaryKeyConstraint("experiment_id", name=op.f("pk_conclusions")),
    )


def downgrade() -> None:
    op.drop_table("conclusions")
