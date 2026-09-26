"""Materialized dataset versions (DS-005).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_versions",
        sa.Column("dataset_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("feature_set_version", sa.String(length=64), nullable=False),
        sa.Column("target_set_version", sa.String(length=64), nullable=True),
        sa.Column("bar_set_ids", sa.JSON(), nullable=False),
        sa.Column("quality_run_ids", sa.JSON(), nullable=False),
        sa.Column("start_utc", sa.BigInteger(), nullable=False),
        sa.Column("end_utc", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("git_sha", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("dataset_id", name=op.f("pk_dataset_versions")),
    )


def downgrade() -> None:
    op.drop_table("dataset_versions")
