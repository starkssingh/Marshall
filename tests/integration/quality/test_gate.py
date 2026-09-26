"""DQ-007: the quality gate decides which partitions a dataset may use."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from xq.core.time import utc_now
from xq.core.types import Timeframe
from xq.data.bars import bar_set_dir, build_version
from xq.data.clean import rules_version
from xq.datasets.builder import build_dataset, load_dataset
from xq.quality.gate import QualityGateError, gate_partitions
from xq.quality.validate import validate_source
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import DataSource, QualityResultRecord, QualityRunRecord

RUN = "01QRUN0000000000000000GATE"
D1, D2, D3, D4 = (date(2024, 3, d) for d in (11, 12, 13, 14))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    upgrade_to_head(engine, REPO / "migrations")
    results = {
        D1: {"tick.ordering": "pass", "bar.zero_range": "pass"},
        D2: {"tick.ordering": "pass", "bar.zero_range": "warn", "tick.spikes": "warn"},
        D3: {"tick.ordering": "fail", "bar.ohlc_consistency": "fail", "tick.spikes": "warn"},
    }
    with session_factory(engine)() as session:
        session.add(
            DataSource(
                source_id="mt5_primary",
                vendor="v",
                feed_type="broker_ticks",
                venue="tbd",
                price_type="bid_ask_ticks",
                clock_convention="NY+7",
                notes="",
            )
        )
        session.flush()
        now = utc_now()
        session.add(
            QualityRunRecord(
                run_id=RUN,
                scope="trading_day",
                source_id="mt5_primary",
                start_utc=now,
                end_utc=now,
                rules_version="c1-x",
                build_version="b1-x",
                includes_vault=False,
                git_sha="t",
                config_hash="t",
                report_path="r",
                summary_json={},
                created_at=now,
            )
        )
        session.flush()
        session.add_all(
            QualityResultRecord(
                run_id=RUN,
                partition_id=f"{day}",
                check_id=check,
                trading_day=day,
                severity="major",
                metric_value=0.0,
                warn_threshold=None,
                fail_threshold=None,
                status=status,
                details_json={},
            )
            for day, checks in results.items()
            for check, status in checks.items()
        )
        session.commit()
    yield engine
    engine.dispose()


def test_pass_and_warn_days_are_included_with_warnings_listed(engine: Engine) -> None:
    decision = gate_partitions(engine, RUN, [D2, D1, D1], {})
    assert decision.included == (D1, D2)
    assert decision.excluded == ()
    assert dict(decision.warnings) == {D2: ("bar.zero_range", "tick.spikes")}
    entry = decision.manifest_entry()
    assert entry["warn_partitions"] == [
        {"trading_day": "2024-03-12", "checks": ["bar.zero_range", "tick.spikes"]}
    ]
    assert entry["included_partitions"] == 2


def test_fail_days_are_refused_unless_excluded(engine: Engine) -> None:
    with pytest.raises(
        QualityGateError, match=r"2024-03-13 \(bar.ohlc_consistency, tick.ordering\)"
    ):
        gate_partitions(engine, RUN, [D1, D3], {})
    decision = gate_partitions(engine, RUN, [D1, D3], {D3: "bad export, reviewed"})
    assert decision.included == (D1,)
    (exclusion,) = decision.excluded
    assert exclusion.trading_day == D3
    assert exclusion.reason == "bad export, reviewed"
    assert exclusion.failing_checks == ("bar.ohlc_consistency", "tick.ordering")


def test_unvalidated_days_are_refused(engine: Engine) -> None:
    with pytest.raises(QualityGateError, match=r"without results .* 2024-03-14"):
        gate_partitions(engine, RUN, [D1, D4], {})
    decision = gate_partitions(engine, RUN, [D1, D4], {D4: "not validated, excluded"})
    assert decision.excluded[0].failing_checks == ()


def test_voluntary_exclusion_of_a_passing_day(engine: Engine) -> None:
    decision = gate_partitions(engine, RUN, [D1, D2], {D1: "known gap in the export"})
    assert decision.included == (D2,)
    assert decision.excluded[0].reason == "known gap in the export"


def test_unknown_run(engine: Engine) -> None:
    with pytest.raises(QualityGateError, match="not found"):
        gate_partitions(engine, "01QRUN000000000000000NOPE", [D1], {})


def test_the_gate_blocks_a_partition_with_an_injected_ohlc_error(
    tmp_path: Path, clean_week_dir: Path
) -> None:
    """Plan acceptance for DQ-007: an injected OHLC error blocks its partition from datasets."""
    cfg = config(tmp_path)
    engine = validated_pipeline(cfg, clean_week_dir, run_id="01QRUN0000000000000000OK01")
    build = build_version(cfg.bars_config(), rules_version(cfg.cleaning_config()))
    path = bar_set_dir(cfg, "mt5_primary", Timeframe.M1, build) / "year=2024" / "month=03"
    part = path / "part.parquet"
    bars = pd.read_parquet(part)
    victim = bars.index[bars["trading_day"] == date(2024, 3, 13)][100]
    bars.loc[victim, "mid_high"] = bars.loc[victim, "mid_low"] - 1.0  # high below low
    pq.write_table(pa.Table.from_pandas(bars, preserve_index=False), part)

    validate_source(cfg, engine, "mt5_primary", run_id="01QRUN0000000000000000BAD1", git_sha="t")
    spec = dataset_spec(quality_run_id="01QRUN0000000000000000BAD1")
    with pytest.raises(QualityGateError, match=r"2024-03-13 \(bar.ohlc_consistency\)"):
        build_dataset(cfg, engine, spec, git_sha="t")

    excluded = spec.model_copy(
        update={
            "exclusions": [
                *spec.exclusions,
                *dataset_spec(
                    exclusions=[{"trading_day": "2024-03-13", "reason": "injected OHLC error"}]
                ).exclusions,
            ]
        }
    )
    ref = build_dataset(cfg, engine, excluded, git_sha="t")
    assert ref.manifest["excluded_partitions"] == [
        {
            "trading_day": "2024-03-13",
            "reason": "injected OHLC error",
            "failing_checks": ["bar.ohlc_consistency"],
        }
    ]
    features = load_dataset(cfg, ref.dataset_id)
    # Bars of trading day 13 March close in (21:00 UTC on the 12th, 21:00 UTC on the 13th].
    day_start = pd.Timestamp("2024-03-12 21:00", tz="UTC")
    day_end = pd.Timestamp("2024-03-13 21:00", tz="UTC")
    assert not ((features.index > day_start) & (features.index <= day_end)).any()
    engine.dispose()
