"""TGT-001: datasets with a target set — storage, label columns, locking and the schema guard."""

from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType

import pandas as pd
import pytest
from sqlalchemy import Engine

import xq.targets.kinds as kinds
from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import config
from helpers.targets import STUB_KIND
from xq.core.config import AppConfig
from xq.core.errors import ConfigError
from xq.datasets.builder import build_dataset, load_dataset
from xq.targets.base import TargetSetChangedError, target_values
from xq.tracking.db import session_factory
from xq.tracking.models import TargetSetRecord

STUB = {
    "targets.stub.v1.kind": "stub",
    "targets.stub.v1.horizons": ["15m", "1h"],
    "targets.stub.v1.price_refs": ["long", "mid"],
}
TARGET_SET = {"name": "stub", "version": "v1"}


@pytest.fixture(autouse=True)
def stub_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kinds, "TARGET_KINDS", MappingProxyType({"stub": STUB_KIND}))


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return config(tmp_path, **STUB)


@pytest.fixture
def engine(cfg: AppConfig, clean_week_dir: Path) -> Iterator[Engine]:
    engine = validated_pipeline(cfg, clean_week_dir)
    yield engine
    engine.dispose()


def test_targets_are_stored_apart_from_features(cfg: AppConfig, engine: Engine) -> None:
    ref = build_dataset(cfg, engine, dataset_spec(target_set=TARGET_SET), git_sha="t")
    assert {p.name for p in ref.path.iterdir()} >= {"features.parquet", "targets.parquet"}
    features = load_dataset(cfg, ref.dataset_id)
    targets = load_dataset(cfg, ref.dataset_id, "targets")
    assert not set(targets["target"].unique()) & set(features.columns)
    assert sorted(targets["target"].unique()) == [
        "stub_long_15m",
        "stub_long_1h",
        "stub_mid_15m",
        "stub_mid_1h",
    ]
    assert len(targets) == 4 * len(features)
    one = target_values(targets, "stub_long_1h")
    assert one.index.equals(features.index)
    assert (one["label_start"] >= one.index).all()
    assert (one["label_end"] == one.index + pd.Timedelta("1h")).all()

    manifest = ref.manifest
    assert set(manifest["files"]) == {"features.parquet", "targets.parquet"}
    assert manifest["targets"] == sorted(targets["target"].unique())
    assert manifest["target_rows"] == len(targets)
    assert manifest["code_versions"]["targets:stub.v1"] == 1
    assert manifest["columns"]["targets"]["value"] == "float64"
    with session_factory(engine)() as session:
        locked = session.get(TargetSetRecord, ("stub", "v1"))
    assert locked is not None
    assert locked.hash == manifest["target_set_hash"]


def test_rebuild_with_targets_is_reproducible(cfg: AppConfig, engine: Engine) -> None:
    first = build_dataset(cfg, engine, dataset_spec(target_set=TARGET_SET), git_sha="t")
    second = build_dataset(cfg, engine, dataset_spec(target_set=TARGET_SET), git_sha="t")
    assert second.reproduced
    assert second.manifest["sha256"] == first.manifest["sha256"]


def test_a_changed_definition_needs_a_new_version(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    build_dataset(cfg, engine, dataset_spec(target_set=TARGET_SET), git_sha="t")
    changed = config(tmp_path, **{**STUB, "targets.stub.v1.horizons": ["15m", "4h"]})
    with pytest.raises(TargetSetChangedError, match="new version"):
        build_dataset(changed, engine, dataset_spec(target_set=TARGET_SET), git_sha="t")


def test_unknown_target_sets_and_kinds(cfg: AppConfig, engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown target set stub\.v2"):
        build_dataset(
            cfg, engine, dataset_spec(target_set={"name": "stub", "version": "v2"}), git_sha="t"
        )
    other = config(tmp_path, **{**STUB, "targets.stub.v1.kind": "nope"})
    with pytest.raises(ConfigError, match="unknown target kind 'nope'"):
        build_dataset(other, engine, dataset_spec(target_set=TARGET_SET), git_sha="t")
