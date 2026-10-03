"""Performance history per bundle (MREG-004).

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-28

One row per bundle, source (backtest, vault, paper, live) and trading day: the day's net return
on the capital, its net P&L and the trades closed, with the run that produced it. Rows are
appended, never changed or deleted (ADR 0060).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS = {
    "trg_bundle_performance_no_update": "BEFORE UPDATE ON bundle_performance BEGIN "
    "SELECT RAISE(ABORT, 'the performance history is append-only'); END",
    "trg_bundle_performance_no_delete": "BEFORE DELETE ON bundle_performance BEGIN "
    "SELECT RAISE(ABORT, 'the performance history is append-only'); END",
}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        raise NotImplementedError(
            f"the registry's enforcing triggers exist for SQLite only, not {dialect} (PAPER-004)"
        )
    op.create_table(
        "bundle_performance",
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("net_return", sa.Float(), nullable=False),
        sa.Column("net_pnl", sa.Float(), nullable=True),
        sa.Column("trades", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(length=26), nullable=True),
        sa.Column("recorded_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bundle_id"],
            ["strategy_bundles.bundle_id"],
            name=op.f("fk_bundle_performance_bundle_id_strategy_bundles"),
        ),
        sa.PrimaryKeyConstraint(
            "bundle_id", "source", "trading_day", name=op.f("pk_bundle_performance")
        ),
    )
    for name, body in TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("bundle_performance")
