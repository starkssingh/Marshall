"""ML-009: a stored fold model carries its model card, and a reload reproduces the pipeline's
test predictions within 1e-9; tampered artifacts and library drift are refused."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import load_config
from xq.core.seeds import make_rng
from xq.models.persistence import (
    ARTIFACT,
    CARD,
    PersistenceError,
    library_versions,
    load_model,
    max_abs_difference,
    save_trained_fold,
    training_data_hash,
)
from xq.models.pipeline import FoldData, TrainedFold, train_fold
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter

PIPELINE = load_config("research", config_dir=REPO / "config").ml_config().pipeline
FOREST = {"n_estimators": 50, "max_depth": 4, "min_samples_leaf": 20, "max_samples": 0.5}


def data() -> FoldData:
    rng = make_rng(21)
    n = 1500
    times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    x = pd.DataFrame(rng.normal(size=(n, 3)), index=times, columns=["a", "b", "c"])
    y = pd.Series(((x["a"] - x["c"] + rng.normal(0, 1, n)) > 0).astype(float), index=times)
    return FoldData(x, y, pd.Series(times + pd.Timedelta(hours=3), index=times))


def trained(family: str, params: dict[str, object]) -> tuple[FoldData, TrainedFold]:
    sample = data()
    fold = WalkForwardSplitter(
        WalkForwardConfig.model_validate({"min_train": "30D", "test_len": "10D", "embargo": "1D"})
    ).split(pd.DatetimeIndex(sample.x.index), sample.label_end)[0]
    result = train_fold(
        family, fold, sample, PIPELINE, embargo=pd.Timedelta(days=1), seed=13, params=params
    )
    assert result is not None
    return sample, result


@pytest.mark.parametrize(
    ("family", "params"), [("logistic", {"C": 0.5}), ("random_forest", FOREST)]
)
def test_a_reload_reproduces_the_test_predictions_within_1e_9(
    family: str, params: dict[str, object], tmp_path: Path
) -> None:
    sample, fold = trained(family, params)
    card = save_trained_fold(
        tmp_path, fold, sample, dataset_id="ds-synthetic", feature_set="core.v1"
    )
    model = load_model(tmp_path)
    test = sample.x.loc[fold.predictions.index]
    again = model.predict(test)
    assert max_abs_difference(again["p_raw"], fold.predictions["p_raw"]) <= 1e-9
    assert max_abs_difference(again["p_cal"], fold.predictions["p_cal"]) <= 1e-9
    assert model.card == card


def test_the_model_card_says_what_the_model_is(tmp_path: Path) -> None:
    sample, fold = trained("logistic", {"C": 0.5, "l1_ratio": 0.2})
    save_trained_fold(tmp_path, fold, sample, dataset_id="ds-synthetic", feature_set="core.v1")
    card = json.loads((tmp_path / CARD).read_text())
    assert card["family"] == "logistic"
    assert card["params"] == {"C": 0.5, "l1_ratio": 0.2}
    assert card["seed"] == 13
    assert card["features"] == ["a", "b", "c"]
    assert (card["dataset_id"], card["feature_set"], card["fold_id"]) == (
        "ds-synthetic",
        "core.v1",
        fold.fold_id,
    )
    assert card["train_end"] == str(fold.train_end)
    assert card["n_fit"] == fold.n_fit
    assert card["calibration"] == "platt"  # fewer than 1,000 validation rows
    assert card["training_data_sha256"] == training_data_hash(sample, fold.fit_index)
    assert set(card["library_versions"]) == {"python", "numpy", "pandas", "scikit-learn",
                                             "joblib", "optuna"}  # fmt: skip
    # the hash covers the training data: changing one fitting row changes it
    shifted = FoldData(sample.x.copy(), sample.y, sample.label_end)
    shifted.x.iloc[0, 0] += 1
    assert training_data_hash(shifted, fold.fit_index) != card["training_data_sha256"]


def test_tampered_artifacts_and_library_drift_are_refused(tmp_path: Path) -> None:
    sample, fold = trained("logistic", {"C": 0.5})
    save_trained_fold(tmp_path, fold, sample, dataset_id="ds-synthetic", feature_set="core.v1")
    card = json.loads((tmp_path / CARD).read_text())
    card["library_versions"]["scikit-learn"] = "0.0.1"
    (tmp_path / CARD).write_text(json.dumps(card))
    with pytest.raises(PersistenceError, match="other library versions"):
        load_model(tmp_path)
    assert (
        load_model(tmp_path, allow_version_drift=True).card.library_versions["scikit-learn"]
        == "0.0.1"
    )
    with (tmp_path / ARTIFACT).open("ab") as handle:
        handle.write(b"\0")
    with pytest.raises(PersistenceError, match="SHA-256"):
        load_model(tmp_path, allow_version_drift=True)
    assert library_versions()["scikit-learn"] != "0.0.1"
    assert max_abs_difference([1.0, np.nan], [1.0, 2.0]) == float("inf")
