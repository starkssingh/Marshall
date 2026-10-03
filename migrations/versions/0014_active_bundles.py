"""The active bundle of each environment, with its history (MREG-005).

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-28

Each row activates a bundle in an environment (``activate``) or restores the one before
(``rollback``); the latest row of an environment is its active bundle. Rows are append-only, so the
pointer's whole history is kept. A bundle can be activated only in an environment its status
allows: ``paper`` needs paper, live_eligible or live; ``prod`` needs live (ADR 0060).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS = {
    "trg_active_bundles_no_update": "BEFORE UPDATE ON active_bundles BEGIN "
    "SELECT RAISE(ABORT, 'the active-bundle history is append-only'); END",
    "trg_active_bundles_no_delete": "BEFORE DELETE ON active_bundles BEGIN "
    "SELECT RAISE(ABORT, 'the active-bundle history is append-only'); END",
    "trg_active_bundles_status": "BEFORE INSERT ON active_bundles BEGIN "
    "SELECT RAISE(ABORT, 'the bundle''s status does not allow this environment') "
    "WHERE NOT EXISTS (SELECT 1 FROM strategy_bundles WHERE bundle_id = NEW.bundle_id AND ("
    "(NEW.environment = 'paper' AND status IN ('paper', 'live_eligible', 'live')) OR "
    "(NEW.environment = 'prod' AND status = 'live'))); END",
}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        raise NotImplementedError(
            f"the registry's enforcing triggers exist for SQLite only, not {dialect} (PAPER-004)"
        )
    op.create_table(
        "active_bundles",
        sa.Column("activation_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("previous_bundle_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("activated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bundle_id"],
            ["strategy_bundles.bundle_id"],
            name=op.f("fk_active_bundles_bundle_id_strategy_bundles"),
        ),
        sa.PrimaryKeyConstraint("activation_id", name=op.f("pk_active_bundles")),
    )
    for name, body in TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("active_bundles")
