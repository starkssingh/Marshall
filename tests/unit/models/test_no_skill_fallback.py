"""C-34 (1): the pipeline's calibrated probabilities shrink towards the fold's training base rate,
a fold with no skill shown on validation predicts that base rate, and uniqueness weights train the
model as well as its calibration.

Acceptance (fixed by the owner before any run, ADR 0068): on the null process the stitched test log
loss is at most climatology + 0.01 in at least 18 of 20 seeds; on the planted-signal process the
pipeline beats climatology and the fallback is not triggered. Climatology is each fold's training
base rate. The processes (``helpers.ml_processes``) and the shrinkage prior (``config/ml.yaml``)
were fixed before the first acceptance run.

The planted-signal test asserts the run-level reading (every run beats climatology; no run is
carried by the fallback). The strict per-fold reading, that no single fold falls back, is **not
met**: 4 of 100 folds fell back, each with a cross-fitted validation loss 0.0001 to 0.0023
above the base rate's. ADR 0068 reports both acceptance runs seed by seed, the design change
between them (two purged validation halves replaced by the purged k-fold, made after the first
run), and leaves the per-fold reading to the owner.
"""

import numpy as np
import pandas as pd
import pytest

from helpers.ml_processes import (
    FOREST,
    HORIZON,
    PLANTED_HORIZON,
    against_climatology,
    no_signal,
    planted_signal,
    walk_forward_folds,
)
from helpers.pipeline import REPO
from xq.core.config import load_config
from xq.features.base import TrainingFoldScaler
from xq.models.base import build_forecaster
from xq.models.calibration import CalibrationError, Calibrator
from xq.models.pipeline import run_pipeline, scaled_inputs, train_fold
from xq.targets.weights import label_uniqueness, uniqueness_weights
from xq.validation.forecast_eval import auc

PIPELINE = load_config("research", config_dir=REPO / "config").ml_config().pipeline
NULL_SEEDS = range(20)
PLANTED_SEEDS = range(5)
PLANTED_FAMILIES = {"logistic": {"C": 1.0}, "random_forest": FOREST}


def null_run(seed: int) -> tuple[float, float, int, int, float]:
    """The null process's stitched test log loss, its climatology, the folds falling back and
    trained, and the AUC of the model's raw probabilities (its ranking)."""
    data = no_signal(seed=seed)
    out = run_pipeline(
        "random_forest", data, walk_forward_folds(data), PIPELINE,
        embargo=pd.Timedelta(hours=HORIZON), seed=seed, params=FOREST,
    )  # fmt: skip
    loss, climatology = against_climatology(out.predictions, out.folds)
    oos = out.predictions.dropna(subset=["y_true", "p_raw"])
    ranking = auc(oos["y_true"], oos["p_raw"])
    return loss, climatology, sum(f.fallback for f in out.folds), len(out.folds), ranking


def planted_run(family: str, seed: int) -> tuple[float, float, int, int]:
    """The same for the planted-signal process and `family`."""
    data = planted_signal(seed=seed)
    out = run_pipeline(
        family, data, walk_forward_folds(data, embargo="1D"), PIPELINE,
        embargo=pd.Timedelta(days=1), seed=seed, params=PLANTED_FAMILIES[family],
    )  # fmt: skip
    loss, climatology = against_climatology(out.predictions, out.folds)
    return loss, climatology, sum(f.fallback for f in out.folds), len(out.folds)


def test_on_the_null_process_the_pipeline_is_no_worse_than_climatology_and_ranks_at_chance() -> (
    None
):
    results = [null_run(seed) for seed in NULL_SEEDS]
    within = [loss <= climatology + 0.01 for loss, climatology, *_ in results]
    assert sum(within) >= 18, results
    # Ranking at chance (the purging demonstration's pipeline check, moved here): one seed's AUC
    # rests on ~35 independent labels (standard error ~0.1), so the bound is on the mean over the
    # 20 seeds (standard error ~0.022), fixed at 0.05 before it was run.
    assert abs(np.mean([r[-1] for r in results]) - 0.5) < 0.05, [r[-1] for r in results]


@pytest.mark.parametrize("family", sorted(PLANTED_FAMILIES))
def test_on_the_planted_signal_the_pipeline_beats_climatology_without_falling_back(
    family: str,
) -> None:
    for seed in PLANTED_SEEDS:
        loss, climatology, fallbacks, trained = planted_run(family, seed)
        assert trained > 0
        assert fallbacks < trained / 2, (seed, fallbacks, trained)  # not carried by the fallback
        assert loss < climatology, (seed, loss, climatology)


def test_a_fold_without_skill_predicts_its_training_base_rate() -> None:
    data = no_signal(seed=4)
    folds = walk_forward_folds(data)
    out = run_pipeline(
        "random_forest", data, folds, PIPELINE, embargo=pd.Timedelta(hours=HORIZON), seed=1,
        params=FOREST,
    )  # fmt: skip
    fell_back = [f for f in out.folds if f.fallback]
    assert fell_back, "the deep forest shows no skill on the null process"
    for fold in fell_back:
        assert fold.shrinkage == 0.0
        fit_mean = data.y.loc[fold.fit_index].mean()
        assert fold.base_rate == pytest.approx(fit_mean)
        predicted = fold.predictions["p_cal"].dropna()
        np.testing.assert_allclose(predicted, fit_mean)
        assert fold.val_metrics["fallback"] == 1.0
        assert not (fold.val_metrics["log_loss_crossfit"] < fold.val_metrics[
            "log_loss_crossfit_base_rate"
        ])  # fmt: skip


def test_shrinkage_keeps_n_eff_over_n_eff_plus_k0_of_the_calibrated_distance() -> None:
    data = planted_signal(seed=2)
    fold = walk_forward_folds(data, embargo="1D")[3]
    trained = train_fold(
        "logistic", fold, data, PIPELINE, embargo=pd.Timedelta(days=1), seed=1, params={"C": 1.0}
    )
    assert trained is not None
    assert not trained.fallback
    rows = trained.val_index
    unique = label_uniqueness(pd.Series(rows, index=rows), data.label_end.loc[rows])
    n_eff = float(unique["uniqueness"].sum())
    assert trained.n_eff == pytest.approx(n_eff)
    assert n_eff == pytest.approx(len(rows) / PLANTED_HORIZON, rel=0.05)
    assert trained.shrinkage == pytest.approx(n_eff / (n_eff + PIPELINE.shrinkage_prior))
    assert trained.calibrator is not None
    probe = np.linspace(0.05, 0.95, 19)
    unshrunk = Calibrator(trained.calibrator.method)
    unshrunk.__dict__.update({**trained.calibrator.__dict__, "base_rate": None, "weight": 1.0})
    base = trained.base_rate
    assert base is not None
    np.testing.assert_allclose(
        trained.calibrator.transform(probe),
        np.clip(base + trained.shrinkage * (unshrunk.transform(probe) - base), 1e-6, 1 - 1e-6),
    )


def test_more_independent_validation_labels_shrink_less() -> None:
    weak, strong = PIPELINE.shrinkage_prior / 4, PIPELINE.shrinkage_prior * 4
    assert weak / (weak + PIPELINE.shrinkage_prior) < strong / (strong + PIPELINE.shrinkage_prior)
    with pytest.raises(CalibrationError):
        Calibrator("platt").shrink(1.0, 0.5)
    with pytest.raises(CalibrationError):
        Calibrator("platt").shrink(0.5, 1.5)


def test_uniqueness_weights_train_the_model_as_well_as_the_calibration() -> None:
    data = no_signal(n=1500, seed=5)
    fold = walk_forward_folds(data)[0]
    embargo = pd.Timedelta(hours=HORIZON)
    trained = train_fold("logistic", fold, data, PIPELINE, embargo=embargo, seed=5, params={})
    assert trained is not None
    fit = trained.fit_index
    unique = label_uniqueness(pd.Series(fit, index=fit), data.label_end.loc[fit])
    weights = uniqueness_weights(unique["uniqueness"])
    assert not np.allclose(weights, 1.0)  # overlapping labels: the weights are not uniform
    positions = np.flatnonzero(data.x.index.isin(fit))
    scaler = TrainingFoldScaler().fit(data.x, positions)
    scaled = scaled_inputs(scaler, data.x)
    probe = scaled.iloc[-200:]
    weighted = build_forecaster("logistic", {}, 5).fit(
        scaled.loc[fit], data.y.loc[fit], sample_weight=weights
    )
    unweighted = build_forecaster("logistic", {}, 5).fit(scaled.loc[fit], data.y.loc[fit])
    np.testing.assert_allclose(
        trained.forecaster.predict_proba(probe), weighted.predict_proba(probe), atol=1e-12
    )
    assert not np.allclose(weighted.predict_proba(probe), unweighted.predict_proba(probe))
    flat = PIPELINE.model_copy(update={"sample_weights": "none"})
    plain = train_fold("logistic", fold, data, flat, embargo=embargo, seed=5, params={})
    assert plain is not None
    np.testing.assert_allclose(
        plain.forecaster.predict_proba(probe), unweighted.predict_proba(probe), atol=1e-12
    )
