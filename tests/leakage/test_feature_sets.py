"""DS-006 parametrized suite: every registered feature set passes the leakage harness.

Runs on synthetic 15-minute base bars with 1-hour context bars. A feature set added to
`xq.datasets.base_features.FEATURE_SETS` is picked up automatically.
"""

from collections.abc import Callable

import pandas as pd
import pytest

from xq.core.types import Timeframe
from xq.datasets.base_features import FEATURE_SETS, FeatureContext
from xq.datasets.leakage import FeatureFn, Inputs

AssertCausal = Callable[[FeatureFn, Inputs], None]
CONTEXT = FeatureContext(Timeframe.M15, (Timeframe.H1,))


@pytest.mark.parametrize("key", sorted(FEATURE_SETS), ids=lambda k: f"{k[0]}.{k[1]}")
def test_feature_set_is_causal(
    key: tuple[str, str], bar_inputs: dict[str, pd.DataFrame], assert_causal: AssertCausal
) -> None:
    definition = FEATURE_SETS[key]
    inputs = {"base": bar_inputs["base"], "1h": bar_inputs["h1"]}
    assert_causal(lambda data: definition.compute(data, CONTEXT), inputs)
