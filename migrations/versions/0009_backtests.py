"""Backtest records (BT-010).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtests",
        sa.Column("backtest_id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("cost_model_version", sa.String(length=128), nullable=False),
        sa.Column("start", sa.BigInteger(), nullable=False),
        sa.Column("end", sa.BigInteger(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("ledger_path", sa.Text(), nullable=True),
        sa.Column("report_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], name=op.f("fk_backtests_run_id_runs")),
        sa.PrimaryKeyConstraint("backtest_id", name=op.f("pk_backtests")),
    )


def downgrade() -> None:
    op.drop_table("backtests")
