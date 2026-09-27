"""DS-006 parametrized suite: every causal primitive (DS-003) and the availability join (DS-002)
passes the leakage harness on synthetic bars. New features join this suite (CLAUDE.md)."""

from collections.abc import Callable

import pandas as pd
import pytest

from helpers.primitive_cases import PRIMITIVE_CASES
from xq.datasets.asof import asof_join
from xq.datasets.leakage import FeatureFn, Inputs
from xq.datasets.primitives import resample_causal

AssertCausal = Callable[[FeatureFn, Inputs], None]


def on_base_close(name: str) -> FeatureFn:
    def fn(inputs: Inputs) -> pd.DataFrame:
        frame = inputs["base"]
        close = pd.Series(
            frame["close"].to_numpy(),
            index=pd.DatetimeIndex(frame["available_at_utc"], name="decision_time"),
        )
        return pd.DataFrame({name: PRIMITIVE_CASES[name](close)})

    return fn


@pytest.mark.parametrize("name", sorted(PRIMITIVE_CASES))
def test_primitive_is_causal(
    name: str, bar_inputs: dict[str, pd.DataFrame], assert_causal: AssertCausal
) -> None:
    assert_causal(on_base_close(name), bar_inputs)


def test_context_join_on_availability_is_causal(
    bar_inputs: dict[str, pd.DataFrame], assert_causal: AssertCausal
) -> None:
    def fn(inputs: Inputs) -> pd.DataFrame:
        decisions = pd.DataFrame({"decision_time": inputs["base"]["available_at_utc"]})
        joined = asof_join(decisions, inputs["h1"], on_right="available_at_utc", prefix="h1_")
        return joined.set_index("decision_time")

    assert_causal(fn, bar_inputs)


def test_resampled_bars_published_late_are_causal(
    bar_inputs: dict[str, pd.DataFrame], assert_causal: AssertCausal
) -> None:
    """15-minute bars published 2 minutes after they end, indexed by bar start, resampled to
    1 hour with the publication latency, and joined onto decision times on availability."""
    delay = pd.Timedelta(minutes=2)
    base = bar_inputs["base"].copy()
    base["available_at_utc"] = base["available_at_utc"] + delay

    def fn(inputs: Inputs) -> pd.DataFrame:
        frame = inputs["base"]
        starts = pd.DatetimeIndex(frame["bar_start_utc"])
        close = pd.Series(frame["close"].to_numpy(), index=starts)
        hourly = resample_causal(close, "1h", "last", latency=delay)
        right = pd.DataFrame({"available_at_utc": hourly.index, "close": hourly.to_numpy()})
        decisions = pd.DataFrame({"decision_time": frame["available_at_utc"]})
        joined = asof_join(decisions, right, on_right="available_at_utc", prefix="h1_")
        return joined.set_index("decision_time")

    assert_causal(fn, {**bar_inputs, "base": base})
