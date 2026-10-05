"""FEAT-001: feature registration, specs, feature sets, multi-timeframe joins, the definition
lock, and the normalization rule (training-fold scalers and trailing z-scores only)."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import FeatureInstanceConfig, FeatureSetConfig, load_config
from xq.core.errors import ConfigError
from xq.core.types import Timeframe
from xq.datasets.base_features import FeatureContext
from xq.features.base import (
    BarContext,
    FeatureParams,
    TrainingFoldScaler,
    column,
    contiguous,
    frame,
    register_feature,
    trailing_zscore,
)
from xq.features.registry import (
    FeatureSetChangedError,
    build_registry,
    compute_feature_set,
    definition_hash,
    feature,
    feature_specs,
    lock_feature_set,
)
from xq.tracking.db import create_db_engine, upgrade_to_head

CFG = load_config("research", config_dir=REPO / "config")
SESSIONS = CFG.sessions_config()


class _Lag(FeatureParams):
    bars: int


def _change(bars: pd.DataFrame, params: _Lag, context: BarContext) -> pd.DataFrame:
    """Close minus the close `bars` bars earlier."""
    close = column(bars, "close")
    return frame(bars, value=close - close.shift(params.bars))


def _two(bars: pd.DataFrame, params: _Lag, context: BarContext) -> pd.DataFrame:
    """Two sub-columns."""
    close = column(bars, "close")
    return frame(bars, up=close * 0 + params.bars, down=-close * 0 - params.bars)


CHANGE = register_feature(
    name="change",
    version=1,
    family="price",
    params=_Lag,
    lookback=lambda p: p.bars + 1,
    warmup=lambda p: p.bars + 1,
)(_change)
TWO = register_feature(
    name="two", version=2, family="time", params=_Lag, lookback=lambda p: 1, warmup=lambda p: 1
)(_two)
REGISTRY = build_registry([CHANGE, TWO])


def bars(timeframe: Timeframe, n: int, start: str = "2024-03-11 00:00") -> pd.DataFrame:
    begin = pd.date_range(start, periods=n, freq=timeframe.duration, tz="UTC")
    close = 2000 + np.arange(n, dtype=np.float64)
    return pd.DataFrame(
        {
            "bar_start_utc": begin,
            "available_at_utc": begin + timeframe.duration,
            "open": close - 0.5,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "tick_count": np.full(n, 10),
            "spread_mean": 0.3,
            "spread_max": 0.5,
            "spread_close": 0.3,
            "n_flagged": 0,
            "n_excluded": 0,
            "trading_day": [d.date() for d in begin],
        }
    )


def test_registration_refuses_bad_names_families_and_versions() -> None:
    def make(**changes: object) -> None:
        kwargs: dict[str, object] = {
            "name": "ok",
            "version": 1,
            "family": "price",
            "params": _Lag,
            "lookback": lambda p: 1,
            "warmup": lambda p: 1,
            **changes,
        }
        register_feature(**kwargs)(_change)  # type: ignore[arg-type]

    make()
    for name in ("tgt_ret", "fwd_x", "Bad", "1st", "a-b"):
        with pytest.raises(ConfigError):
            make(name=name)
    with pytest.raises(ConfigError, match="family"):
        make(family="magic")
    with pytest.raises(ConfigError, match="code version"):
        make(version=0)
    with pytest.raises(ConfigError, match="twice"):
        build_registry([CHANGE, CHANGE])
    with pytest.raises(ConfigError, match="unknown feature"):
        feature("nope", REGISTRY)


def test_specs_carry_the_plan_s_fields_and_validate_parameters() -> None:
    spec = CHANGE.spec(FeatureInstanceConfig(feature="change", params={"bars": 3}))
    assert (spec.name, spec.version, spec.family, spec.timeframe) == ("change", 1, "price", None)
    assert (spec.params, spec.lookback, spec.warmup) == ({"bars": 3}, 4, 4)
    assert (spec.inputs, spec.column) == (("base",), "change_3")
    hourly = CHANGE.spec(
        FeatureInstanceConfig(feature="change", params={"bars": 2}, timeframe="1h", column="c2")
    )
    assert (hourly.inputs, hourly.column) == (("1h",), "c2")
    with pytest.raises(ConfigError, match="invalid parameters"):
        CHANGE.spec(FeatureInstanceConfig(feature="change", params={"bars": 3, "extra": 1}))
    with pytest.raises(ConfigError, match="invalid parameters"):
        CHANGE.spec(FeatureInstanceConfig(feature="change", params={}))
    twice = FeatureSetConfig(
        features=[
            FeatureInstanceConfig(feature="change", params={"bars": 3}),
            FeatureInstanceConfig(feature="change", params={"bars": 3}),
        ]
    )
    with pytest.raises(ConfigError, match="more than once"):
        feature_specs(twice, REGISTRY)


def test_a_feature_set_joins_context_timeframes_on_availability() -> None:
    base, hourly = bars(Timeframe.M15, 40), bars(Timeframe.H1, 10)
    context = FeatureContext(Timeframe.M15, (Timeframe.H1,), SESSIONS)
    definition = FeatureSetConfig(
        base_columns=False,
        features=[
            FeatureInstanceConfig(feature="change", params={"bars": 1}),
            FeatureInstanceConfig(feature="two", params={"bars": 5}),
            FeatureInstanceConfig(feature="change", params={"bars": 2}, timeframe="1h"),
        ],
    )
    specs = feature_specs(definition, REGISTRY)
    out = compute_feature_set(specs, {"base": base, "1h": hourly}, context, registry=REGISTRY)
    assert list(out.columns) == [
        "change_1",
        "two_5_up",
        "two_5_down",
        "mtf_1h_change_2",
        "mtf_1h_available_at",
    ]
    assert out.index.equals(pd.DatetimeIndex(base["available_at_utc"]))
    # the hourly value at a decision is that of the latest hourly bar available by then
    provenance = out["mtf_1h_available_at"]
    assert (provenance.dropna() <= out.index[provenance.notna()]).all()
    decision = pd.Timestamp("2024-03-11 03:15", tz="UTC")
    assert provenance.loc[decision] == pd.Timestamp("2024-03-11 03:00", tz="UTC")
    assert out.loc[decision, "mtf_1h_change_2"] == 2.0  # hourly closes rise by 1 a bar
    assert pd.isna(out["mtf_1h_change_2"].iloc[0])  # no hourly bar is available yet
    with pytest.raises(ConfigError, match="does not load"):
        compute_feature_set(specs, {"base": base}, context, registry=REGISTRY)
    # the base columns of base.v1 come first when asked for
    with_base = compute_feature_set(
        specs, {"base": base, "1h": hourly}, context, base_columns=True, registry=REGISTRY
    )
    assert {"close", "ctx_1h_close", "in_london"} <= set(with_base.columns)
    assert list(with_base.columns[-5:]) == list(out.columns)


def test_the_definition_is_locked_on_first_use(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'meta.sqlite'}")
    upgrade_to_head(engine, REPO / "migrations")
    one = FeatureSetConfig(features=[FeatureInstanceConfig(feature="change", params={"bars": 1})])
    other = FeatureSetConfig(features=[FeatureInstanceConfig(feature="change", params={"bars": 2})])
    # a feature's code version is part of the definition
    bumped = build_registry(
        [register_feature(name="change", version=2, family="price", params=_Lag,
                          lookback=lambda p: 1, warmup=lambda p: 1)(_change)]
    )  # fmt: skip
    assert definition_hash(one, REGISTRY) != definition_hash(one, bumped)
    assert definition_hash(one, REGISTRY) != definition_hash(other, REGISTRY)
    digest = lock_feature_set(engine, "probe", "v1", one, registry=REGISTRY)
    assert lock_feature_set(engine, "probe", "v1", one, registry=REGISTRY) == digest
    with pytest.raises(FeatureSetChangedError, match="new version"):
        lock_feature_set(engine, "probe", "v1", other, registry=REGISTRY)
    engine.dispose()


def test_contiguity_marks_breaks_and_gaps() -> None:
    frame_ = bars(Timeframe.H1, 5)
    frame_.loc[3:, "bar_start_utc"] += pd.Timedelta(hours=2)
    assert contiguous(frame_, Timeframe.H1).tolist() == [False, True, True, False, True]


def test_trailing_z_scores_are_causal_and_need_a_full_window() -> None:
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 10.0])
    z = trailing_zscore(x, 3)
    assert z.iloc[:2].isna().all()
    assert z.iloc[2] == pytest.approx(1.0)  # (3 - 2) / 1
    assert trailing_zscore(x.iloc[:4], 3).equals(z.iloc[:4])  # truncation invariant
    assert trailing_zscore(pd.Series([2.0, 2.0, 2.0]), 3).isna().all()  # no spread, no score


def test_a_training_fold_scaler_only_sees_its_training_rows() -> None:
    features = pd.DataFrame({"a": [1.0, 2.0, 3.0, 100.0, 200.0], "b": [5.0, 5.0, 5.0, 1.0, 2.0]})
    scaler = TrainingFoldScaler().fit(features, [0, 1, 2])
    out = scaler.transform(features)
    assert out["a"].tolist() == pytest.approx([-1.0, 0.0, 1.0, 98.0, 198.0])
    assert out["b"].isna().all()  # constant in training: no scale
    changed = features.assign(a=[1.0, 2.0, 3.0, -7.0, 9.0])  # test rows do not move the scale
    refit = TrainingFoldScaler().fit(changed, [0, 1, 2])
    assert refit.transform(changed)["a"].iloc[:3].tolist() == out["a"].iloc[:3].tolist()
    with pytest.raises(ValueError, match="fit the scaler"):
        TrainingFoldScaler().transform(features)
    with pytest.raises(ValueError, match="training rows"):
        TrainingFoldScaler().fit(features, [])
