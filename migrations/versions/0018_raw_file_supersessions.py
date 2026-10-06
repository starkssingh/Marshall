"""Raw files superseded by a re-export of the same source and period (ADR 0071).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_file_supersessions",
        sa.Column("superseded_raw_file_id", sa.String(length=32), nullable=False),
        sa.Column("superseding_raw_file_id", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("period_start_utc", sa.BigInteger(), nullable=False),
        sa.Column("period_end_utc", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ingest_run_id", sa.String(length=26), nullable=False),
        sa.Column("recorded_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingest_run_id"],
            ["ingest_runs.run_id"],
            name=op.f("fk_raw_file_supersessions_ingest_run_id_ingest_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_raw_file_supersessions_source_id_data_sources"),
        ),
        sa.ForeignKeyConstraint(
            ["superseded_raw_file_id"],
            ["raw_files.raw_file_id"],
            name=op.f("fk_raw_file_supersessions_superseded_raw_file_id_raw_files"),
        ),
        sa.ForeignKeyConstraint(
            ["superseding_raw_file_id"],
            ["raw_files.raw_file_id"],
            name=op.f("fk_raw_file_supersessions_superseding_raw_file_id_raw_files"),
        ),
        sa.PrimaryKeyConstraint("superseded_raw_file_id", name=op.f("pk_raw_file_supersessions")),
    )


def downgrade() -> None:
    op.drop_table("raw_file_supersessions")
