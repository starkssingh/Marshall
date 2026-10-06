"""ADR 0071 and DQ-007: the exclusion list removes its days from every dataset of the source,
records them in the manifest, changes the dataset id, and is refused where the dataset's quality
run contradicts it."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, update

from helpers.datasets import QUALITY_RUN, dataset_spec, validated_pipeline
from helpers.pipeline import config
from xq.core.config import AppConfig, ExclusionsConfig
from xq.datasets.builder import build_dataset, config_digest, load_dataset
from xq.quality.gate import QualityGateError
from xq.tracking.db import session_factory
from xq.tracking.models import QualityResultRecord

DAY = date(2024, 3, 13)
AFTER_REEXPORT = "01QRUN000000000000000AFTER"
RULE = {"check": "cal.missing_open_data", "max_missing_market_share": 0.20}


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    return config(tmp_path_factory.mktemp("exclusion_list"))


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, clean_week_dir: Path) -> Iterator[Engine]:
    engine = validated_pipeline(cfg, clean_week_dir)
    yield engine
    engine.dispose()


def listing(cfg: AppConfig, *days: date, share: float = 0.35) -> AppConfig:
    """`cfg` whose exclusion list names `days` for the test source."""
    exclusions = ExclusionsConfig.model_validate(
        {
            "rule": RULE,
            "days": {
                "mt5_primary": [
                    {
                        "trading_day": day.isoformat(),
                        "reason": "hours still missing after the re-export",
                        "missing_market_share": share,
                        "quality_run_id": AFTER_REEXPORT,
                    }
                    for day in days
                ]
            },
        }
    )
    return cfg.model_copy(update={"exclusions": exclusions})


def set_missing_share(engine: Engine, day: date, share: float) -> None:
    """Stand-in for a real damaged day: the gating run's cal.missing_open_data metric."""
    with session_factory(engine)() as session:
        session.execute(
            update(QualityResultRecord)
            .where(
                QualityResultRecord.run_id == QUALITY_RUN,
                QualityResultRecord.check_id == "cal.missing_open_data",
                QualityResultRecord.trading_day == day,
            )
            .values(metric_value=share)
        )
        session.commit()


def test_an_empty_list_changes_nothing(cfg: AppConfig) -> None:
    assert cfg.excluded_days("mt5_primary") == []
    assert config_digest(listing(cfg), dataset_spec()) == config_digest(cfg, dataset_spec())


def test_a_listed_day_the_run_contradicts_is_refused(cfg: AppConfig, engine: Engine) -> None:
    # A share at the rule, not above it, does not support excluding the day.
    set_missing_share(engine, DAY, 0.20)
    with pytest.raises(QualityGateError, match=r"does not support: 2024-03-13 \(20\.0%\)"):
        build_dataset(listing(cfg, DAY), engine, dataset_spec(), git_sha="x")


def test_a_listed_day_is_excluded_and_recorded(cfg: AppConfig, engine: Engine) -> None:
    set_missing_share(engine, DAY, 0.35)
    listed = listing(cfg, DAY, date(2014, 10, 13))  # the second lies outside the dataset
    ref = build_dataset(listed, engine, dataset_spec(), git_sha="x")
    full = build_dataset(cfg, engine, dataset_spec(), git_sha="x")
    assert ref.dataset_id != full.dataset_id  # the list is part of the config digest
    assert ref.manifest["excluded_partitions"] == [
        {
            "trading_day": "2024-03-13",
            "reason": "exclusion list (config/exclusions.yaml): hours still missing after the "
            "re-export",
            "failing_checks": [],
        }
    ]
    features = load_dataset(listed, ref.dataset_id)
    start = pd.Timestamp("2024-03-12 21:00", tz="UTC")
    end = pd.Timestamp("2024-03-13 21:00", tz="UTC")
    assert not ((features.index > start) & (features.index <= end)).any()
    assert len(load_dataset(cfg, full.dataset_id)) > len(features)

    # A reason the spec gives itself takes precedence over the list's.
    own = {"trading_day": "2024-03-13", "reason": "spec exclusion"}
    spec_ref = build_dataset(listed, engine, dataset_spec(exclusions=[own]), git_sha="x")
    assert spec_ref.manifest["excluded_partitions"] == [{**own, "failing_checks": []}]
