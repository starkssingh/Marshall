"""FEAT-001 end to end: a dataset built with the configured feature set ``core.v1`` (the base
columns, every engineered feature and the multi-timeframe context) on a synthetic week; its
definition enters the dataset id and is locked on first use."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import config
from xq.core.config import AppConfig
from xq.datasets.builder import build_dataset, load_dataset
from xq.features.registry import (
    FEATURES,
    FRAMEWORK_VERSION,
    FeatureSetChangedError,
    feature_specs,
    output_prefix,
)
from xq.tracking.db import session_factory
from xq.tracking.models import FeatureSetRecord

CORE = {"name": "core", "version": "v1"}


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    return config(tmp_path_factory.mktemp("feature_set_dataset"))


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, clean_week_dir: Path) -> Iterator[Engine]:
    engine = validated_pipeline(cfg, clean_week_dir)
    yield engine
    engine.dispose()


def spec(**changes: object) -> object:
    return dataset_spec(feature_set=CORE, context_timeframes=["1h", "4h", "1d"], **changes)


def test_a_dataset_carries_the_base_columns_and_every_configured_feature(
    cfg: AppConfig, engine: Engine
) -> None:
    ref = build_dataset(cfg, engine, spec(), git_sha="t")  # type: ignore[arg-type]
    features = load_dataset(cfg, ref.dataset_id)
    definition = cfg.feature_set_config("core", "v1")
    expected = {output_prefix(s) + s.column for s in feature_specs(definition)}
    columns = set(features.columns)
    assert {"close", "ctx_1h_close", "ctx_1d_close", "in_london"} <= columns  # base.v1
    produced = {c for c in columns if any(c == e or c.startswith(f"{e}_") for e in expected)}
    assert {
        e for e in expected if not any(c == e or c.startswith(f"{e}_") for c in produced)
    } == set()
    versions = ref.manifest["code_versions"]
    assert versions["features:core.v1"] == FRAMEWORK_VERSION
    used = {i.feature for i in definition.features}
    assert {k for k in versions if k.startswith("feature:")} == {f"feature:{n}" for n in used}
    assert used == set(FEATURES)
    # rebuilt from the same definition: the same id and content
    again = build_dataset(cfg, engine, spec(), git_sha="t")  # type: ignore[arg-type]
    assert (again.dataset_id, again.reproduced) == (ref.dataset_id, True)
    with session_factory(engine)() as session:
        assert session.get(FeatureSetRecord, ("core", "v1")) is not None


def test_a_changed_definition_under_the_same_version_is_refused(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    build_dataset(cfg, engine, spec(), git_sha="t")  # type: ignore[arg-type]
    definition = cfg.feature_set_config("core", "v1")
    changed = definition.model_copy(update={"features": definition.features[:-1]})
    altered = cfg.model_copy(update={"features": {"core": {"v1": changed}}})
    with pytest.raises(FeatureSetChangedError, match="new version"):
        build_dataset(altered, engine, spec(), git_sha="t")  # type: ignore[arg-type]
