"""FEAT-001 and C-33 (3) end to end: a dataset built with the configured feature set ``core.v1``
on seventeen synthetic weeks. The builder reads each timeframe's warm-up bars from before the
dataset's start (the longest lookback of the set, computed from its specs), so every feature is
defined from the first trading day; nothing is filled. Missing or gate-failed warm-up bars stop
the build. The set's definition enters the dataset id and is locked on first use."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import numpy as np
import pytest
from sqlalchemy import Engine, select

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import config
from helpers.ticks import dense_ticks, write_mt5
from xq.core.config import AppConfig
from xq.datasets.builder import FeatureWarmupError, build_dataset, load_dataset
from xq.datasets.spec import DatasetSpec, SetRef
from xq.features.registry import (
    FEATURES,
    FRAMEWORK_VERSION,
    FeatureSetChangedError,
    GatedFeatureError,
    feature_specs,
    model_inputs,
    output_prefix,
    warmup_bars,
)
from xq.quality.gate import QualityGateError
from xq.tracking.db import session_factory
from xq.tracking.models import FeatureSetRecord, QualityResultRecord

CORE = {"name": "core", "version": "v1"}
#: Labor Day 2024: the synthetic ticks run through it, so the quality run fails it (closed market).
LABOR_DAY = {"trading_day": "2024-09-02", "reason": "US holiday, synthetic ticks"}


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    return config(tmp_path_factory.mktemp("feature_set_dataset"))


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    directory = tmp_path_factory.mktemp("seventeen_weeks")
    ticks = dense_ticks("2024-07-07 22:00", "2024-11-09 00:00", seed=61, mean_interval_s=10)
    write_mt5(ticks, directory / "XAUUSD_seventeen_weeks.csv")
    engine = validated_pipeline(cfg, directory)
    yield engine
    engine.dispose()


def spec(**changes: Any) -> DatasetSpec:
    fields: dict[str, Any] = {
        "feature_set": CORE,
        "context_timeframes": ["1h", "4h", "1d"],
        "start": "2024-10-27T21:00:00Z",  # trading day 2024-10-28 (17:00 New York, EDT)
        "end": "2024-11-08T20:00:00Z",
        "exclusions": [LABOR_DAY],
        **changes,
    }
    return dataset_spec(**fields)


def core_columns(cfg: AppConfig, columns: list[str]) -> list[str]:
    """The columns `core.v1`'s features write (not the base columns, not provenance)."""
    expected = [
        output_prefix(s) + s.column for s in feature_specs(cfg.feature_set_config("core", "v1"))
    ]
    return [
        c
        for c in columns
        if not c.endswith("available_at") and any(c == e or c.startswith(f"{e}_") for e in expected)
    ]


def test_the_warm_up_is_the_longest_lookback_of_each_timeframe(cfg: AppConfig) -> None:
    needs = warmup_bars(cfg, SetRef(name="core", version="v1"))
    specs = feature_specs(cfg.feature_set_config("core", "v1"))
    assert set(needs) == {"base", "1h", "4h", "1d"}
    for name, need in needs.items():
        own = [max(s.lookback, s.warmup) for s in specs if s.inputs[0] == name]
        assert need == max(own)
    assert needs["1d"] >= 77  # Wilder's RSI 14: the bars carrying 99 % of its weight
    assert warmup_bars(cfg, SetRef(name="base", version="v1")) == {}


def test_core_v1_has_no_missing_values_from_the_first_trading_day(
    cfg: AppConfig, engine: Engine
) -> None:
    ref = build_dataset(cfg, engine, spec(), git_sha="t")
    features = load_dataset(cfg, ref.dataset_id)
    columns = core_columns(cfg, list(features.columns))
    definition = cfg.feature_set_config("core", "v1")
    expected = {output_prefix(s) + s.column for s in feature_specs(definition)}
    assert {
        e for e in expected if not any(c == e or c.startswith(f"{e}_") for c in columns)
    } == set()
    first = features.index[0]
    assert str(first) == "2024-10-27 22:15:00+00:00"  # the first decision after the 18:00 open
    values = features[columns].to_numpy(np.float64)
    missing = [columns[j] for j in np.flatnonzero(np.isnan(values).any(axis=0))]
    assert missing == [], f"missing feature values from the first trading day: {missing}"
    # the gated VWAP distance is computed and stored, but refused as a model input (C-33 (4))
    assert features["session_vwap_96"].notna().all()
    assert "session_vwap_96" not in model_inputs(features, definition).columns
    with pytest.raises(GatedFeatureError):
        model_inputs(features, definition, ["session_vwap_96"])
    # the context bars of the base columns are there too
    assert features[["ctx_1h_close", "ctx_4h_close", "ctx_1d_close"]].notna().all().all()
    versions = ref.manifest["code_versions"]
    assert versions["features:core.v1"] == FRAMEWORK_VERSION == 2
    used = {i.feature for i in [*definition.features, *definition.gated]}
    assert {k for k in versions if k.startswith("feature:")} == {f"feature:{n}" for n in used}
    assert used == set(FEATURES)
    # rebuilt from the same definition: the same id and content
    again = build_dataset(cfg, engine, spec(), git_sha="t")
    assert (again.dataset_id, again.reproduced) == (ref.dataset_id, True)
    with session_factory(engine)() as session:
        assert session.get(FeatureSetRecord, ("core", "v1")) is not None


def test_missing_warm_up_bars_stop_the_build(cfg: AppConfig, engine: Engine) -> None:
    # Starting in August leaves about 18 daily bars before the start; 1d RSI needs 77.
    early = spec(start="2024-08-04T21:00:00Z", end="2024-08-09T20:00:00Z", exclusions=[])
    with pytest.raises(FeatureWarmupError, match=r"needs \d+ 1d bars"):
        build_dataset(cfg, engine, early, git_sha="t")


def test_gate_failed_warm_up_bars_stop_the_build(cfg: AppConfig, engine: Engine) -> None:
    # Labor Day fails the quality run and lies inside the 1d warm-up: without its exclusion the
    # quality gate refuses the build, as for any other bars.
    with pytest.raises(QualityGateError, match="2024-09-02"):
        build_dataset(cfg, engine, spec(exclusions=[]), git_sha="t")


def test_a_changed_definition_under_the_same_version_is_refused(
    cfg: AppConfig, engine: Engine
) -> None:
    build_dataset(cfg, engine, spec(), git_sha="t")
    definition = cfg.feature_set_config("core", "v1")
    changed = definition.model_copy(update={"features": definition.features[:-1]})
    altered = cfg.model_copy(update={"features": {"core": {"v1": changed}}})
    with pytest.raises(FeatureSetChangedError, match="new version"):
        build_dataset(altered, engine, spec(), git_sha="t")


def test_the_labor_day_exclusion_is_a_real_failure(cfg: AppConfig, engine: Engine) -> None:
    # guards the fixture: the excluded day is the only failing day of the quality run
    with session_factory(engine)() as session:
        failing = set(
            session.scalars(
                select(QualityResultRecord.trading_day).where(QualityResultRecord.status == "fail")
            )
        )
    assert failing == {date(2024, 9, 2)}
