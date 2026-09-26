"""The built-in base feature set (DS-005, extended by DS-007).

Until the feature library exists (FEAT-001, Sprint 7), a dataset's features are its *base
columns*: the decision bar's own values and the latest context bars available at the decision
time, joined on availability. Nothing here is engineered or fitted; everything is known when the
decision bar becomes available.

A feature set is a pure function of named input frames (``"base"`` plus one frame per context
timeframe, each with ``available_at_utc``) returning one row per decision time, so the leakage
harness (DS-006) can test it. Its `code_version` is part of every dataset id.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pandas as pd

from xq.core.errors import ConfigError
from xq.core.types import Timeframe
from xq.datasets.asof import asof_join
from xq.datasets.leakage import Inputs
from xq.datasets.spec import SetRef

AVAILABLE_AT = "available_at_utc"
DECISION_TIME = "decision_time"
BASE_INPUT = "base"

#: Bar columns carried as base features (price columns are of the dataset's price basis).
BAR_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "tick_count",
    "spread_mean",
    "spread_max",
    "spread_close",
    "n_flagged",
    "n_excluded",
)
#: Columns of each context bar joined onto the decision time.
CONTEXT_COLUMNS = ("open", "high", "low", "close", "tick_count")


@dataclass(frozen=True)
class FeatureContext:
    """What a feature set needs besides its input frames."""

    base_timeframe: Timeframe
    context_timeframes: tuple[Timeframe, ...]


FeatureSetFn = Callable[[Inputs, FeatureContext], pd.DataFrame]


@dataclass(frozen=True)
class FeatureSetDef:
    """A versioned feature set: its compute function and code version."""

    name: str
    version: str
    code_version: int
    compute: FeatureSetFn
    description: str


def decision_index(base: pd.DataFrame) -> pd.DatetimeIndex:
    """Decision times of the base bars: each bar's ``available_at``."""
    return pd.DatetimeIndex(base[AVAILABLE_AT], name=DECISION_TIME)


def context_prefix(tf: Timeframe) -> str:
    """Column prefix of a context timeframe, e.g. ``ctx_1h_``."""
    return f"ctx_{tf.value}_"


def base_v1(inputs: Inputs, context: FeatureContext) -> pd.DataFrame:
    """Decision-bar columns plus the latest available bar of each context timeframe."""
    base = inputs[BASE_INPUT]
    index = decision_index(base)
    features = pd.DataFrame(
        {column: base[column].to_numpy() for column in BAR_COLUMNS}, index=index
    )
    decisions = pd.DataFrame({DECISION_TIME: index})
    for tf in context.context_timeframes:
        joined = asof_join(
            decisions,
            inputs[tf.value],
            on_right=AVAILABLE_AT,
            prefix=context_prefix(tf),
            columns=list(CONTEXT_COLUMNS),
        )
        for column in joined.columns.drop(DECISION_TIME):
            features[column] = joined[column].to_numpy()
    return features


FEATURE_SETS: Mapping[tuple[str, str], FeatureSetDef] = {
    ("base", "v1"): FeatureSetDef(
        name="base",
        version="v1",
        code_version=1,
        compute=base_v1,
        description="decision-bar values and context bars joined on availability",
    ),
}


def feature_set(ref: SetRef) -> FeatureSetDef:
    """The definition of feature set `ref`.

    Raises:
        ConfigError: if no such feature set exists.
    """
    try:
        return FEATURE_SETS[(ref.name, ref.version)]
    except KeyError:
        known = ", ".join(f"{n}.{v}" for n, v in sorted(FEATURE_SETS)) or "none"
        raise ConfigError(f"unknown feature set {ref}; available: {known}") from None
