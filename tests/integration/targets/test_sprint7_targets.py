"""TGT-003 … TGT-006 end to end: a dataset built with each new target set on a synthetic week
stores every target with its label window; labels are only where the market was open, and every
``label_end`` lies in its window."""

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import config
from xq.core.config import AppConfig
from xq.datasets.builder import build_dataset, load_dataset
from xq.targets.base import market_horizon
from xq.targets.weights import label_uniqueness

SETS = ["realized_vol", "excursions", "barriers", "derived"]
DAY = pd.Timedelta(hours=23)


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    return config(tmp_path_factory.mktemp("sprint7_targets"))


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, clean_week_dir: Path) -> Iterator[Engine]:
    engine = validated_pipeline(cfg, clean_week_dir)
    yield engine
    engine.dispose()


@pytest.mark.parametrize("name", SETS)
def test_a_dataset_stores_every_target_of_the_set(
    cfg: AppConfig, engine: Engine, name: str
) -> None:
    ref = build_dataset(
        cfg, engine, dataset_spec(target_set={"name": name, "version": "v1"}), git_sha="t"
    )
    targets = load_dataset(cfg, ref.dataset_id, "targets")
    features = load_dataset(cfg, ref.dataset_id)
    definition = cfg.target_set(name, "v1")
    assert ref.manifest["code_versions"][f"targets:{name}.v1"] == 1
    assert all(str(t).startswith("tgt_") for t in targets["target"].unique())  # never features
    for target, one in targets.groupby("target"):
        assert len(one) == len(features), target
        labelled = one.loc[one["value"].notna()]
        assert len(labelled) > 0.5 * len(one), target
        horizon = max(market_horizon(h, DAY) for h in definition.horizons)
        assert (labelled["label_start"] >= labelled.index).all()
        assert (labelled["label_end"] >= labelled["label_start"]).all()
        # within the window: at most the longest horizon, plus latency, weekend and delay
        assert (labelled["label_end"] - labelled.index < horizon + pd.Timedelta(days=3)).all()
    if name == "barriers":
        labels = targets.loc[targets["target"] == "tgt_tb_long_1h", "value"].dropna()
        assert set(np.unique(labels)) <= {-1.0, 0.0, 1.0}
        assert (targets.loc[targets["target"].str.endswith("_amb"), "value"].dropna() == 0).all()
        one = targets.loc[targets["target"] == "tgt_tb_long_1h"].dropna(subset=["value"])
        weights = label_uniqueness(one["label_start"], one["label_end"])
        assert weights["uniqueness"].between(0, 1, inclusive="right").all()
        assert (weights["weight_end"] >= one["label_end"]).all()
