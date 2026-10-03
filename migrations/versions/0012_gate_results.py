"""Gate results and gate-enforced status transitions (MREG-002).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28

``gate_results`` holds one row per evaluation of a gate on a subject (a model version or a
strategy bundle): the criteria, the values, whether it passed, who evaluated it and the evidence.
It is append-only. The status trigger of ``model_versions`` is replaced: a status moves one step
along the promotion order, or to ``retired``, and a promotion needs the subject's **latest** result
of the matching gate to have passed (candidate R1, validated R2, vault_passed and paper R3,
live_eligible R4). ``live`` is never allowed here: it needs the GATE-004 human review (ADR 0060).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (from, to) pairs a status may move along; any status but retired may also be retired.
STEPS = (
    ("draft", "candidate"),
    ("candidate", "validated"),
    ("validated", "vault_passed"),
    ("vault_passed", "paper"),
    ("paper", "live_eligible"),
)
#: The gate a promotion to each status needs.
GATE_FOR = {
    "candidate": "R1",
    "validated": "R2",
    "vault_passed": "R3",
    "paper": "R3",
    "live_eligible": "R4",
}


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
    "trg_gate_results_no_update": "BEFORE UPDATE ON gate_results BEGIN "
    "SELECT RAISE(ABORT, 'gate results are append-only'); END",
    "trg_gate_results_no_delete": "BEFORE DELETE ON gate_results BEGIN "
    "SELECT RAISE(ABORT, 'gate results are append-only'); END",
    "trg_model_versions_status": status_trigger(
        "model_versions", "model_version_id", "model_version"
    ),
}
#: The trigger 0011 created, restored on downgrade.
PREVIOUS_STATUS_TRIGGER = (
    "BEFORE UPDATE OF status ON model_versions WHEN NEW.status <> OLD.status BEGIN "
    "SELECT RAISE(ABORT, 'status transition not allowed') "
    "WHERE NOT (OLD.status <> 'retired' AND NEW.status = 'retired'); END"
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        raise NotImplementedError(
            f"the registry's enforcing triggers exist for SQLite only, not {dialect} (PAPER-004)"
        )
    op.create_table(
        "gate_results",
        sa.Column("gate_result_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("gate", sa.String(length=8), nullable=False),
        sa.Column("criteria_json", sa.JSON(), nullable=False),
        sa.Column("values_json", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("evaluator", sa.String(length=128), nullable=False),
        sa.Column("evidence_paths", sa.JSON(), nullable=False),
        sa.Column("gates_hash", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("gate_result_id", name=op.f("pk_gate_results")),
    )
    op.execute("DROP TRIGGER trg_model_versions_status")
    for name, body in TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute(f"CREATE TRIGGER trg_model_versions_status {PREVIOUS_STATUS_TRIGGER}")
    op.drop_table("gate_results")
