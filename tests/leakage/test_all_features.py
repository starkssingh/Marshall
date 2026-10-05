"""FEAT-001: the leakage harness (DS-006) auto-applied to every registered feature.

Every spec of every feature set in ``config/features.yaml`` is computed alone through the same
code the dataset builder runs (`compute_feature_set`, base-timeframe and multi-timeframe joins
alike) on eight synthetic weeks of 15m bars with 1h, 4h and 1d context bars, and must pass
truncation invariance, future perturbation and the availability audit. A feature added to the
registry joins automatically once a configured set uses it; a registered feature that no
configured set uses fails `test_every_registered_feature_is_checked`, so no feature can reach a
dataset unchecked. The last tests show the harness catching a planted leak through this path.
"""

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import FeatureInstanceConfig, FeatureSetConfig, load_config
from xq.core.types import Timeframe
from xq.datasets.base_features import FeatureContext
from xq.datasets.leakage import FeatureFn, Inputs, check_feature_causality
from xq.features.base import BarContext, FeatureParams, column, frame, register_feature
from xq.features.registry import (
    FEATURES,
    build_registry,
    compute_feature_set,
    feature_specs,
    unchecked_features,
)

AssertCausal = Callable[[FeatureFn, Inputs], None]
CFG = load_config("research", config_dir=REPO / "config")
CONTEXT = FeatureContext(
    Timeframe.M15, (Timeframe.H1, Timeframe.H4, Timeframe.D1), CFG.sessions_config()
)
SPECS = [
    (f"{name}.{version}:{spec.timeframe or 'base'}:{spec.column}", spec)
    for name, versions in sorted(CFG.features.items())
    for version, definition in sorted(versions.items())
    for spec in feature_specs(definition)
]


@pytest.mark.parametrize(("label", "spec"), SPECS, ids=[s[0] for s in SPECS])
def test_every_configured_feature_passes_the_harness(
    label: str, spec: object, feature_inputs: dict[str, pd.DataFrame]
) -> None:
    def fn(data: Inputs) -> pd.DataFrame:
        return compute_feature_set([spec], data, CONTEXT)  # type: ignore[list-item]

    full = fn(feature_inputs)
    values = full.drop(columns=[c for c in full.columns if c.endswith("available_at")])
    assert np.isfinite(values.to_numpy(np.float64)).any(), "no finite value to test"
    report = check_feature_causality(fn, feature_inputs, n_points=8, seed=11)
    assert report.passed, report.summary()


def test_every_registered_feature_is_checked() -> None:
    assert unchecked_features(CFG) == []


# --- the harness catches a planted leak through the same path --------------------------------


class _Window(FeatureParams):
    window: int


def _peek(bars: pd.DataFrame, params: _Window, context: BarContext) -> pd.DataFrame:
    """A leak: the next bar's close (a negative shift, banned from library code)."""
    return frame(bars, value=column(bars, "close").shift(-1))


LEAKY = register_feature(
    name="peek", version=1, family="price", params=_Window, lookback=lambda p: 1, warmup=lambda p: 1
)(_peek)


@pytest.mark.parametrize("timeframe", [None, Timeframe.H1], ids=["base", "mtf_1h"])
def test_a_planted_leak_fails_the_harness(
    timeframe: Timeframe | None, feature_inputs: dict[str, pd.DataFrame]
) -> None:
    registry = build_registry([LEAKY])
    definition = FeatureSetConfig(
        features=[FeatureInstanceConfig(feature="peek", params={"window": 1}, timeframe=timeframe)]
    )
    (spec,) = feature_specs(definition, registry)
    report = check_feature_causality(
        lambda data: compute_feature_set([spec], data, CONTEXT, registry=registry),
        feature_inputs,
        n_points=8,
        seed=11,
    )
    assert not report.passed
    assert report.checks_failed & {"truncation", "perturbation"}


def test_a_registered_feature_without_a_configured_set_is_unchecked() -> None:
    registry = build_registry([*FEATURES.values(), LEAKY])
    assert unchecked_features(CFG, registry) == ["peek"]
