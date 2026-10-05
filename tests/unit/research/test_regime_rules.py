"""REG-001: rule regimes with cut-offs from training folds only, labelled causally."""

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import load_config
from xq.features.registry import feature_specs, output_prefix
from xq.research.regimes.rules import (
    CompressionRegime,
    RegimeError,
    TrendRegime,
    VolatilityRegime,
    fit_per_fold,
    rule_regimes,
)
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter

CFG = load_config("research", config_dir=REPO / "config")
RULES = CFG.regimes_config().rules
INDEX = pd.date_range("2024-03-11", periods=400, freq="15min", tz="UTC")


def frame(**columns: object) -> pd.DataFrame:
    n = len(next(iter(columns.values())))  # type: ignore[arg-type]
    return pd.DataFrame(columns, index=INDEX[:n])


def test_the_configured_columns_are_core_v1_feature_columns() -> None:
    specs = feature_specs(CFG.feature_set_config("core", "v1"))
    names = {output_prefix(s) + s.column for s in specs}
    columns = {
        RULES.volatility.column,
        RULES.trend.efficiency_column,
        RULES.trend.slope_column,
        RULES.compression.ratio_column,
        RULES.compression.bandwidth_column,
    }
    assert columns <= names
    assert RULES.trend.adx_column == "adx_14_adx"  # the ADX sub-column of the adx_14 spec
    assert "adx_14" in names
    assert set(rule_regimes(RULES)) == {"volatility", "trend", "compression"}


def test_volatility_terciles_come_from_the_training_rows() -> None:
    train = frame(ewma_sigma_96=np.arange(1.0, 301.0))  # 1 ... 300
    model = VolatilityRegime(RULES.volatility, 100).fit(train)
    assert model.cutoffs_ == pytest.approx(
        {
            "low": np.quantile(np.arange(1.0, 301.0), 0.3333),
            "high": np.quantile(np.arange(1.0, 301.0), 0.6667),
        }
    )
    out = model.filter(frame(ewma_sigma_96=[5.0, 150.0, 290.0, np.nan, 291.0]))
    assert out["label"].tolist() == ["low", "mid", "high", None, "high"]
    assert out["state"].tolist()[:3] == [0.0, 1.0, 2.0]
    assert out["p_high"].tolist()[2] == 1.0
    assert out["p_low"].tolist()[2] == 0.0
    assert np.isnan(out["p_mid"].iloc[3])
    assert out["regime_age"].tolist()[:3] == [1.0, 1.0, 1.0]
    assert np.isnan(out["regime_age"].iloc[3])  # unknown: the run ends
    assert out["regime_age"].iloc[4] == 1.0
    assert [s.name for s in model.states()] == ["low", "mid", "high"]


def test_trend_needs_every_measure_and_takes_the_slope_s_direction() -> None:
    rng = np.random.default_rng(1)
    train = frame(
        efficiency_ratio_20=rng.uniform(0, 1, 300),
        adx_14_adx=rng.uniform(0, 60, 300),
        ma_slope_t_20=rng.normal(0, 3, 300),
    )
    model = TrendRegime(RULES.trend, 100).fit(train)
    cut = model.cutoffs_
    assert cut is not None
    high_er, high_adx = cut["efficiency"] + 0.01, cut["adx"] + 1
    rows = frame(
        efficiency_ratio_20=[high_er, high_er, high_er, cut["efficiency"] - 0.01],
        adx_14_adx=[high_adx, high_adx, high_adx, high_adx],
        ma_slope_t_20=[cut["slope_abs"] + 1, -cut["slope_abs"] - 1, 0.0, cut["slope_abs"] + 1],
    )
    assert model.filter(rows)["label"].tolist() == ["trend_up", "trend_down", "range", "range"]


def test_compression_and_expansion_need_both_measures() -> None:
    train = frame(
        vol_ratio_16_96=np.linspace(0.5, 1.5, 200), compression_20_96=np.linspace(0, 1, 200)
    )
    model = CompressionRegime(RULES.compression, 100).fit(train)
    rows = frame(vol_ratio_16_96=[0.55, 0.55, 1.45, 1.0], compression_20_96=[0.05, 0.9, 0.95, 0.5])
    assert model.filter(rows)["label"].tolist() == ["compression", "normal", "expansion", "normal"]


def test_labels_are_causal_and_need_a_fit() -> None:
    rng = np.random.default_rng(2)
    data = frame(ewma_sigma_96=rng.uniform(0, 1, 400))
    model = VolatilityRegime(RULES.volatility, 100)
    with pytest.raises(RegimeError, match="fit"):
        model.filter(data)
    model.fit(data.iloc[:200])
    full = model.filter(data)
    for cut in (1, 37, 250):
        pd.testing.assert_frame_equal(model.filter(data.iloc[:cut]), full.iloc[:cut])
    with pytest.raises(RegimeError, match="needs 100 training rows"):
        VolatilityRegime(RULES.volatility, 100).fit(data.iloc[:50])


def test_per_fold_cut_offs_never_see_their_own_test_rows() -> None:
    rng = np.random.default_rng(3)
    times = pd.date_range("2024-01-01", periods=24 * 40, freq="h", tz="UTC")
    data = pd.DataFrame({"ewma_sigma_96": rng.lognormal(0, 0.3, len(times))}, index=times)
    label_end = pd.Series(times + pd.Timedelta(hours=1), index=times)
    folds = WalkForwardSplitter(
        WalkForwardConfig.model_validate({"min_train": "10D", "test_len": "5D", "embargo": "1h"})
    ).split(times, label_end)
    assert len(folds) >= 4
    out = fit_per_fold(VolatilityRegime(RULES.volatility, 100), data, folds)
    for fold in folds:
        rows = out.iloc[fold.test_idx]
        train = data["ewma_sigma_96"].iloc[fold.train_idx]
        assert (rows["fold_id"] == fold.fold_id).all()
        assert rows["cutoff_low"].iloc[0] == pytest.approx(train.quantile(0.3333))
        assert rows["regime_age"].iloc[0] == 1.0  # an age never spans two fits
    # changing a fold's test rows leaves that fold's labels' cut-offs unchanged
    shocked = data.copy()
    shocked.iloc[folds[1].test_idx] *= 10
    again = fit_per_fold(VolatilityRegime(RULES.volatility, 100), shocked, folds)
    first, second = folds[1].test_idx, folds[2].test_idx
    pd.testing.assert_series_equal(again["cutoff_low"].iloc[first], out["cutoff_low"].iloc[first])
    assert not np.allclose(again["cutoff_low"].iloc[second], out["cutoff_low"].iloc[second])
    # rows before the first test window have no state
    assert out["state"].iloc[: folds[0].test_idx[0]].isna().all()
    # a fold with too few training rows gets no state
    sparse = fit_per_fold(VolatilityRegime(RULES.volatility, 10_000), data, folds)
    assert sparse["state"].isna().all()
