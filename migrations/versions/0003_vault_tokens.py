"""Vault gate tokens and the vault access log (DS-004).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vault_tokens",
        sa.Column("token_id", sa.String(length=64), nullable=False),
        sa.Column("secret_sha256", sa.String(length=64), nullable=False),
        sa.Column("bundle_id", sa.String(length=128), nullable=False),
        sa.Column("issued_by", sa.String(length=128), nullable=False),
        sa.Column("issued_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("redeemed_at", sa.BigInteger(), nullable=True),
        sa.Column("redeemed_by_run", sa.String(length=26), nullable=True),
        sa.PrimaryKeyConstraint("token_id", name=op.f("pk_vault_tokens")),
    )
    op.create_table(
        "vault_access_log",
        sa.Column("access_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("window_start_utc", sa.BigInteger(), nullable=False),
        sa.Column("window_end_utc", sa.BigInteger(), nullable=False),
        sa.Column("accessed_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["vault_tokens.token_id"],
            name=op.f("fk_vault_access_log_token_id_vault_tokens"),
        ),
        sa.PrimaryKeyConstraint("access_id", name=op.f("pk_vault_access_log")),
    )


def downgrade() -> None:
    op.drop_table("vault_access_log")
    op.drop_table("vault_tokens")
