"""Data layer provenance tables (DATA-005): sources, instruments, ingest runs, raw files,
clean partitions, cleaning actions, bar sets, bar gaps and spread statistics.

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("vendor", sa.String(length=128), nullable=False),
        sa.Column("feed_type", sa.String(length=32), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("price_type", sa.String(length=32), nullable=False),
        sa.Column("clock_convention", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_data_sources")),
    )
    op.create_table(
        "ingest_runs",
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("git_sha", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_ingest_runs")),
    )
    op.create_table(
        "instruments",
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("tick_size", sa.String(length=40), nullable=False),
        sa.Column("contract_size", sa.String(length=40), nullable=False),
        sa.Column("quote_ccy", sa.String(length=8), nullable=False),
        sa.Column("lot_step", sa.String(length=40), nullable=False),
        sa.Column("min_lot", sa.String(length=40), nullable=False),
        sa.Column("max_lot", sa.String(length=40), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("instrument_id", name=op.f("pk_instruments")),
    )
    op.create_table(
        "bar_sets",
        sa.Column("bar_set_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("basis", sa.String(length=8), nullable=False),
        sa.Column("clean_rules_version", sa.String(length=32), nullable=False),
        sa.Column("build_version", sa.String(length=32), nullable=False),
        sa.Column("start_utc", sa.BigInteger(), nullable=False),
        sa.Column("end_utc", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_bar_sets_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_bar_sets_source_id_data_sources"),
        ),
        sa.PrimaryKeyConstraint("bar_set_id", name=op.f("pk_bar_sets")),
    )
    op.create_table(
        "clean_partitions",
        sa.Column("partition_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("rules_version", sa.String(length=32), nullable=False),
        sa.Column("raw_file_ids", sa.JSON(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("flagged_count", sa.BigInteger(), nullable=False),
        sa.Column("dropped_count", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_clean_partitions_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_clean_partitions_source_id_data_sources"),
        ),
        sa.PrimaryKeyConstraint("partition_id", name=op.f("pk_clean_partitions")),
    )
    op.create_table(
        "raw_files",
        sa.Column("raw_file_id", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("first_ts_utc", sa.BigInteger(), nullable=True),
        sa.Column("last_ts_utc", sa.BigInteger(), nullable=True),
        sa.Column("ingest_run_id", sa.String(length=26), nullable=False),
        sa.Column("ingested_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingest_run_id"],
            ["ingest_runs.run_id"],
            name=op.f("fk_raw_files_ingest_run_id_ingest_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_raw_files_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_raw_files_source_id_data_sources"),
        ),
        sa.PrimaryKeyConstraint("raw_file_id", name=op.f("pk_raw_files")),
        sa.UniqueConstraint("sha256", name=op.f("uq_raw_files_sha256")),
    )
    op.create_table(
        "spread_stats",
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("hour_of_week", sa.Integer(), nullable=False),
        sa.Column("computed_from", sa.BigInteger(), nullable=False),
        sa.Column("computed_to", sa.BigInteger(), nullable=False),
        sa.Column("p50", sa.Float(), nullable=False),
        sa.Column("p90", sa.Float(), nullable=False),
        sa.Column("p99", sa.Float(), nullable=False),
        sa.Column("n", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_spread_stats_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_spread_stats_source_id_data_sources"),
        ),
        sa.PrimaryKeyConstraint(
            "source_id",
            "instrument_id",
            "hour_of_week",
            "computed_from",
            "computed_to",
            name=op.f("pk_spread_stats"),
        ),
    )
    op.create_table(
        "bar_gaps",
        sa.Column("bar_set_id", sa.String(length=64), nullable=False),
        sa.Column("gap_start_utc", sa.BigInteger(), nullable=False),
        sa.Column("gap_end_utc", sa.BigInteger(), nullable=False),
        sa.Column("expected_open", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bar_set_id"], ["bar_sets.bar_set_id"], name=op.f("fk_bar_gaps_bar_set_id_bar_sets")
        ),
        sa.PrimaryKeyConstraint("bar_set_id", "gap_start_utc", name=op.f("pk_bar_gaps")),
    )
    op.create_table(
        "cleaning_actions",
        sa.Column("action_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("partition_id", sa.String(length=64), nullable=False),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("ts_utc", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("original_values_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["partition_id"],
            ["clean_partitions.partition_id"],
            name=op.f("fk_cleaning_actions_partition_id_clean_partitions"),
        ),
        sa.PrimaryKeyConstraint("action_id", name=op.f("pk_cleaning_actions")),
    )


def downgrade() -> None:
    op.drop_table("cleaning_actions")
    op.drop_table("bar_gaps")
    op.drop_table("spread_stats")
    op.drop_table("raw_files")
    op.drop_table("clean_partitions")
    op.drop_table("bar_sets")
    op.drop_table("instruments")
    op.drop_table("ingest_runs")
    op.drop_table("data_sources")
