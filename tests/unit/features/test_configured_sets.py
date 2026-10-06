"""FEAT-001: every configured feature set resolves, and every spec honours its declared warm-up
on synthetic bars of its timeframe (no value before it has the history it declares)."""

import numpy as np
import pandas as pd
import pytest

from helpers.features import CFG, SESSIONS, tick_bars
from helpers.ticks import dense_ticks
from xq.core.types import Timeframe
from xq.datasets.spec import SetRef
from xq.features.base import BarContext
from xq.features.registry import (
    FEATURES,
    FeatureSetCalendarError,
    definition_hash,
    feature,
    feature_specs,
    resolve_feature_set,
)

SPECS = [
    (f"{name}.{version}:{spec.timeframe or 'base'}:{spec.column}", spec)
    for name, versions in sorted(CFG.features.items())
    for version, definition in sorted(versions.items())
    for spec in feature_specs(definition)
]


@pytest.fixture(scope="module")
def bars_by_timeframe() -> dict[Timeframe, pd.DataFrame]:
    ticks = dense_ticks("2024-01-07 23:00", "2024-03-02 00:00", seed=53, mean_interval_s=120)
    return {
        tf: tick_bars(ticks, tf) for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)
    }


def test_the_configured_sets_resolve_on_their_calendar_only() -> None:
    assert SPECS, "config/features.yaml defines no feature"
    calendar = CFG.sessions_config().version
    for name, versions in CFG.features.items():
        for version, config in versions.items():
            ref = SetRef(name=name, version=version)
            if config.calendar != calendar:  # retired with its calendar (ADR 0070)
                with pytest.raises(FeatureSetCalendarError, match="never recomputed"):
                    resolve_feature_set(CFG, ref)
                continue
            definition = resolve_feature_set(CFG, ref)
            assert (definition.name, definition.version) == (name, version)
    assert {spec.name for _, spec in SPECS} == set(FEATURES)


def test_core_v1_is_retired_and_core_v2_repeats_it_on_calendar_s2() -> None:
    # ADR 0067 (5) and ADR 0070: the changed calendar is a new feature-set version.
    v1, v2 = CFG.feature_set_config("core", "v1"), CFG.feature_set_config("core", "v2")
    assert (CFG.sessions_config().version, v1.calendar, v2.calendar) == ("s2", "s1", "s2")
    assert (v2.features, v2.gated, v2.base_columns) == (v1.features, v1.gated, v1.base_columns)
    with pytest.raises(FeatureSetCalendarError, match=r"core\.v1"):
        resolve_feature_set(CFG, SetRef(name="core", version="v1"))
    assert definition_hash(v1) != definition_hash(v2)


@pytest.mark.parametrize(("label", "spec"), SPECS, ids=[s[0] for s in SPECS])
def test_no_value_before_the_declared_warm_up(
    label: str, spec: object, bars_by_timeframe: dict[Timeframe, pd.DataFrame]
) -> None:
    timeframe = spec.timeframe or Timeframe.M15  # type: ignore[attr-defined]
    bars = bars_by_timeframe[timeframe]
    registered = feature(spec.name)  # type: ignore[attr-defined]
    out = registered.on_bars(spec, bars, BarContext(timeframe, SESSIONS))  # type: ignore[arg-type]
    finite = np.isfinite(out.to_numpy(np.float64)).any(axis=1)
    warmup = spec.warmup  # type: ignore[attr-defined]
    assert warmup >= 1
    assert spec.lookback >= 1  # type: ignore[attr-defined]
    assert not finite[: warmup - 1].any(), f"a value before the warm-up of {warmup} bars"
    assert finite.any()
