"""Content-hashed strategy bundles (MREG-003).

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-28

A bundle's id is the SHA-256 of its canonical content (model versions, feature-set version,
signal or strategy configuration, risk configuration, cost-model version), so the same inputs
always give the same id. Its content never changes and it is never deleted; its status moves only
as the gates allow, by the same rule as a model version's (migration 0012, ADR 0060).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same steps and gates as migration 0012 (a migration never imports another).
STEPS = (
    ("draft", "candidate"),
    ("candidate", "validated"),
    ("validated", "vault_passed"),
    ("vault_passed", "paper"),
    ("paper", "live_eligible"),
)
GATE_FOR = {
    "candidate": "R1",
    "validated": "R2",
    "vault_passed": "R3",
    "paper": "R3",
    "live_eligible": "R4",
}
_CONTENT = "bundle_id, name, content_json, origin_run_id, origin_strategy, created_at"


def status_trigger(table: str, id_column: str, kind: str) -> str:
    """The body of a status trigger enforcing the steps and the gates for one subject table."""
    steps = " OR ".join(f"(OLD.status = '{a}' AND NEW.status = '{b}')" for a, b in STEPS)
    gate = " ".join(f"WHEN '{status}' THEN '{g}'" for status, g in GATE_FOR.items())
    return (
        f"BEFORE UPDATE OF status ON {table} WHEN NEW.status <> OLD.status BEGIN "
        "SELECT RAISE(ABORT, 'status transition not allowed') "
        f"WHERE NOT ({steps} OR (OLD.status <> 'retired' AND NEW.status = 'retired')); "
        "SELECT RAISE(ABORT, 'promotion needs a passing gate result') "
        "WHERE NEW.status <> 'retired' AND COALESCE(("
        "SELECT passed FROM gate_results "
        f"WHERE subject_kind = '{kind}' AND subject_id = NEW.{id_column} "
        f"AND gate = (CASE NEW.status {gate} END) "
        "ORDER BY gate_result_id DESC LIMIT 1), 0) = 0; END"
    )


TRIGGERS = {
    "trg_strategy_bundles_immutable": f"BEFORE UPDATE OF {_CONTENT} ON strategy_bundles BEGIN "
    "SELECT RAISE(ABORT, 'a strategy bundle is immutable; register a new bundle'); END",
    "trg_strategy_bundles_no_delete": "BEFORE DELETE ON strategy_bundles BEGIN "
    "SELECT RAISE(ABORT, 'strategy bundles are never deleted'); END",
    "trg_strategy_bundles_status": status_trigger("strategy_bundles", "bundle_id", "bundle"),
}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        raise NotImplementedError(
            f"the registry's enforcing triggers exist for SQLite only, not {dialect} (PAPER-004)"
        )
    op.create_table(
        "strategy_bundles",
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("origin_run_id", sa.String(length=26), nullable=True),
        sa.Column("origin_strategy", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["origin_run_id"], ["runs.run_id"], name=op.f("fk_strategy_bundles_origin_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("bundle_id", name=op.f("pk_strategy_bundles")),
    )
    for name, body in TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("strategy_bundles")
