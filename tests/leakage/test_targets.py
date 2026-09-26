"""DS-006 parametrized suite for targets: every configured target passes the bound checks, and every
target kind's sigma-hat is causal. New target sets and kinds join automatically."""

from collections.abc import Callable

import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import load_config
from xq.core.types import Timeframe
from xq.datasets.leakage import FeatureFn, Inputs, check_target_bounds
from xq.targets.kinds import target_kind

AssertCausal = Callable[[FeatureFn, Inputs], None]
CFG = load_config("research", config_dir=REPO / "config")
DEFINITIONS = [
    (name, version, definition)
    for name, versions in sorted(CFG.targets.items())
    for version, definition in sorted(versions.items())
]
SPECS = [
    (f"{name}.{version}:{spec.name}", definition, spec)
    for name, version, definition in DEFINITIONS
    for spec in target_kind(definition.kind).expand(definition)
]


def quotes_of(week_ticks: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(week_ticks["ts_utc"], unit="ns", utc=True),
            "bid": week_ticks["bid"],
            "ask": week_ticks["ask"],
        }
    )


def close_of(bar_inputs: dict[str, pd.DataFrame]) -> pd.Series:
    base = bar_inputs["base"]
    return pd.Series(base["close"].to_numpy(), index=pd.DatetimeIndex(base["available_at_utc"]))


@pytest.mark.parametrize(("label", "definition", "spec"), SPECS, ids=[s[0] for s in SPECS])
def test_target_uses_only_its_label_window_and_sigma_at_t(
    label: str,
    definition: object,
    spec: object,
    week_ticks: pd.DataFrame,
    bar_inputs: dict[str, pd.DataFrame],
) -> None:
    kind = target_kind(definition.kind)  # type: ignore[attr-defined]
    sigma = kind.sigma(close_of(bar_inputs), definition, Timeframe.M15.duration).dropna()
    report = check_target_bounds(
        lambda quotes, s: kind.compute(spec, quotes, s),  # type: ignore[arg-type]
        quotes_of(week_ticks),
        sigma,
        n_points=15,
        seed=5,
    )
    assert report.passed, report.summary()
    assert report.points, "no finite target values to test"


@pytest.mark.parametrize(
    ("name", "version", "definition"), DEFINITIONS, ids=[f"{d[0]}.{d[1]}" for d in DEFINITIONS]
)
def test_sigma_hat_is_causal(
    name: str,
    version: str,
    definition: object,
    bar_inputs: dict[str, pd.DataFrame],
    assert_causal: AssertCausal,
) -> None:
    kind = target_kind(definition.kind)  # type: ignore[attr-defined]

    def fn(inputs: Inputs) -> pd.DataFrame:
        return pd.DataFrame(
            {"sigma": kind.sigma(close_of(dict(inputs)), definition, Timeframe.M15.duration)}  # type: ignore[arg-type]
        )

    assert_causal(fn, bar_inputs)
