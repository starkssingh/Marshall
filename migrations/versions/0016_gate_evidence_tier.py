"""The evidence tier of a gate result; promotion to paper needs an event-tier R3 (C-27 (3)).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-04

``gate_results.evidence_tier`` records where a result's evidence came from: ``screening`` (the
vectorized screener; R3's risk-limit breaches read from daily losses against the risk profile) or
``event`` (the event backtester, with the risk engine's own decisions). Existing rows are
``screening``. Both status triggers are replaced: besides the steps and the latest passing result
of the matching gate (migrations 0012 and 0013), a promotion to ``paper`` needs that latest R3
result to be on the event tier (the owner's requirement, ADR 0062).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same steps and gates as migrations 0012 and 0013 (a migration never imports another).
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
#: (trigger, table, id column, subject kind) of the two status triggers.
SUBJECTS = (
    ("trg_model_versions_status", "model_versions", "model_version_id", "model_version"),
    ("trg_strategy_bundles_status", "strategy_bundles", "bundle_id", "bundle"),
)


def status_trigger(table: str, id_column: str, kind: str, *, event_tier_for_paper: bool) -> str:
    """The body of a status trigger enforcing the steps and the gates for one subject table."""
    steps = " OR ".join(f"(OLD.status = '{a}' AND NEW.status = '{b}')" for a, b in STEPS)
    gate = " ".join(f"WHEN '{status}' THEN '{g}'" for status, g in GATE_FOR.items())
    latest = (
        f"FROM gate_results WHERE subject_kind = '{kind}' AND subject_id = NEW.{id_column} "
        "AND gate = {gate} ORDER BY gate_result_id DESC LIMIT 1"
    )
    body = (
        f"BEFORE UPDATE OF status ON {table} WHEN NEW.status <> OLD.status BEGIN "
        "SELECT RAISE(ABORT, 'status transition not allowed') "
        f"WHERE NOT ({steps} OR (OLD.status <> 'retired' AND NEW.status = 'retired')); "
        "SELECT RAISE(ABORT, 'promotion needs a passing gate result') "
        "WHERE NEW.status <> 'retired' AND COALESCE(("
        f"SELECT passed {latest.format(gate=f'(CASE NEW.status {gate} END)')}), 0) = 0; "
    )
    if event_tier_for_paper:
        body += (
            "SELECT RAISE(ABORT, 'promotion to paper needs an event-tier R3 result') "
            "WHERE NEW.status = 'paper' AND COALESCE(("
            f"SELECT evidence_tier = 'event' {latest.format(gate="'R3'")}), 0) = 0; "
        )
    return body + "END"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        raise NotImplementedError(
            f"the registry's enforcing triggers exist for SQLite only, not {dialect} (PAPER-004)"
        )
    op.add_column(
        "gate_results",
        sa.Column(
            "evidence_tier", sa.String(length=16), nullable=False, server_default="screening"
        ),
    )
    for name, table, id_column, kind in SUBJECTS:
        op.execute(f"DROP TRIGGER {name}")
        body = status_trigger(table, id_column, kind, event_tier_for_paper=True)
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name, table, id_column, kind in SUBJECTS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
        body = status_trigger(table, id_column, kind, event_tier_for_paper=False)
        op.execute(f"CREATE TRIGGER {name} {body}")
    # The gate_results triggers refuse updates, not schema changes; SQLite drops a column directly.
    op.drop_column("gate_results", "evidence_tier")
