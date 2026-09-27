"""BASE-002 in the leakage suite: rule baseline positions use only signal bars available at each
decision time (truncation invariance, future perturbation and the availability audit).

Signal bars are the synthetic week's 1-hour bars, so the windows are short versions of the board's
daily parameters; the mechanics (trailing windows, stateful entries and exits, as-of placement on
availability) are the same.
"""

from collections.abc import Callable

import pandas as pd
import pytest

from xq.datasets.leakage import FeatureFn, Inputs, check_feature_causality
from xq.models.baselines import (
    SIGNAL_COLUMNS,
    RuleStrategyConfig,
    VolTargetConfig,
    positions_at,
    rule_exposure,
)

AssertCausal = Callable[[FeatureFn, Inputs], None]
VOL = VolTargetConfig(annual_vol=0.10, lookback=6, max_exposure=2.0)
STRATEGIES = {
    "buy_and_hold": RuleStrategyConfig(rule="buy_and_hold"),
    "time_series_momentum": RuleStrategyConfig(rule="time_series_momentum", params={"lookback": 6}),
    "zscore_reversion": RuleStrategyConfig(
        rule="zscore_reversion", params={"lookback": 8, "entry": 1.0, "exit": 0.0}
    ),
    "ma_crossover": RuleStrategyConfig(rule="ma_crossover", params={"fast": 3, "slow": 8}),
    "donchian_breakout": RuleStrategyConfig(
        rule="donchian_breakout",
        params={"entry": 6, "exit": 3, "atr_window": 4, "atr_stop": 2.0},
    ),
}
CASES = list(STRATEGIES.items()) + [
    (f"{name}_vol", s.model_copy(update={"vol_target": True})) for name, s in STRATEGIES.items()
]


def positions(inputs: Inputs, strategy: RuleStrategyConfig) -> pd.DataFrame:
    context = inputs["1h"]
    bars = pd.DataFrame(
        {c: context[c].to_numpy() for c in SIGNAL_COLUMNS},
        index=pd.DatetimeIndex(context["available_at_utc"], name="available_at"),
    )
    exposure = rule_exposure(bars, strategy, vol_target=VOL, periods_per_year=252 * 23)
    decisions = pd.DatetimeIndex(inputs["base"]["available_at_utc"], name="decision_time")
    return positions_at(decisions, exposure).to_frame()


@pytest.mark.parametrize(("name", "strategy"), CASES, ids=[c[0] for c in CASES])
def test_rule_positions_are_causal(
    name: str,
    strategy: RuleStrategyConfig,
    bar_inputs: dict[str, pd.DataFrame],
    assert_causal: AssertCausal,
) -> None:
    inputs = {"base": bar_inputs["base"], "1h": bar_inputs["h1"]}
    full = positions(inputs, strategy)
    if name not in {"buy_and_hold"}:
        assert full["exposure"].abs().gt(0).any(), "the rule never trades on the fixture"
    assert_causal(lambda data: positions(data, strategy), inputs)


def test_placing_signals_by_bar_start_is_caught(bar_inputs: dict[str, pd.DataFrame]) -> None:
    """The planted leak: a signal placed at its bar's start is used an hour before it exists."""

    def leaky(inputs: Inputs) -> pd.DataFrame:
        context = inputs["1h"]
        bars = pd.DataFrame(
            {c: context[c].to_numpy() for c in SIGNAL_COLUMNS},
            index=pd.DatetimeIndex(context["bar_start_utc"], name="available_at"),
        )
        exposure = rule_exposure(bars, STRATEGIES["ma_crossover"])
        decisions = pd.DatetimeIndex(inputs["base"]["available_at_utc"], name="decision_time")
        return positions_at(decisions, exposure).to_frame()

    inputs = {"base": bar_inputs["base"], "1h": bar_inputs["h1"]}
    assert not check_feature_causality(leaky, inputs, seed=4).passed
