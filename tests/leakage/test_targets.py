"""DS-006 parametrized suite for targets: every configured target passes the bound checks, and every
target kind's sigma-hat is causal. New target sets and kinds join automatically.

Horizons are trading time (ADR 0026): targets are computed on the market clock of the calendar,
so decisions near the daily close hold over the break and are tested like any other."""

from collections.abc import Callable
from datetime import date

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import TargetSetConfig, load_config
from xq.core.types import Timeframe
from xq.data.calendar import MarketClock, regular_trading_day
from xq.datasets.leakage import FeatureFn, Inputs, check_target_bounds
from xq.targets.base import TargetSpec
from xq.targets.kinds import target_kind

AssertCausal = Callable[[FeatureFn, Inputs], None]
CFG = load_config("research", config_dir=REPO / "config")
DEFINITIONS = [
    (name, version, definition)
    for name, versions in sorted(CFG.targets.items())
    for version, definition in sorted(versions.items())
]
CLOCK = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 10), date(2024, 3, 25))
TRADING_DAY = regular_trading_day(CFG.sessions_config())
SPECS = [
    (f"{name}.{version}:{spec.name}", definition, spec)
    for name, version, definition in DEFINITIONS
    for spec in target_kind(definition.kind).expand(definition, TRADING_DAY)
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
        lambda quotes, s: kind.compute(spec, quotes, s, CLOCK),  # type: ignore[arg-type]
        quotes_of(week_ticks),
        sigma,
        n_points=15,
        seed=5,
    )
    assert report.passed, report.summary()
    assert report.points, "no finite target values to test"


# A bar-resolved triple barrier (pessimistic same-bar handling, TGT-005) is not in the repository
# configuration, which reads every tick; it must pass the same bounds check.
BAR_BARRIER = CFG.target_set("barriers", "v1").model_copy(
    update={"params": {**CFG.target_set("barriers", "v1").params, "resolution": "15min"}}
)
BAR_SPECS = [
    (f"barriers@15min:{spec.name}", BAR_BARRIER, spec)
    for spec in target_kind(BAR_BARRIER.kind).expand(BAR_BARRIER, TRADING_DAY)
]


@pytest.mark.parametrize(("label", "definition", "spec"), BAR_SPECS, ids=[s[0] for s in BAR_SPECS])
def test_bar_resolved_barriers_use_only_their_label_window(
    label: str,
    definition: TargetSetConfig,
    spec: TargetSpec,
    week_ticks: pd.DataFrame,
    bar_inputs: dict[str, pd.DataFrame],
) -> None:
    kind = target_kind(definition.kind)
    sigma = kind.sigma(close_of(bar_inputs), definition, Timeframe.M15.duration).dropna()
    report = check_target_bounds(
        lambda quotes, s: kind.compute(spec, quotes, s, CLOCK),
        quotes_of(week_ticks),
        sigma,
        n_points=15,
        seed=6,
    )
    assert report.passed, report.summary()
    assert report.points


FORWARD = [s for s in SPECS if s[1].kind == "forward_return"]


@pytest.mark.parametrize(("label", "definition", "spec"), FORWARD, ids=[s[0] for s in FORWARD])
def test_forward_return_exit_is_the_first_quote_after_the_market_time_horizon(
    label: str,
    definition: TargetSetConfig,
    spec: TargetSpec,
    week_ticks: pd.DataFrame,
    bar_inputs: dict[str, pd.DataFrame],
) -> None:
    """``label_end`` is the first quote at or after ``h + latency`` of market time past t."""
    kind = target_kind(definition.kind)
    sigma = kind.sigma(close_of(bar_inputs), definition, Timeframe.M15.duration).dropna()
    quotes = quotes_of(week_ticks)
    out = kind.compute(spec, quotes, sigma, CLOCK)
    labelled = out.loc[out["value"].notna()]
    assert len(labelled), "no labels to test"
    latency = pd.Timedelta(milliseconds=spec.params["execution_latency_ms"])
    decisions = ns(pd.DatetimeIndex(labelled.index))
    intended = pd.DatetimeIndex(
        pd.to_datetime(CLOCK.advance(decisions, (spec.horizon + latency).value), utc=True)
    )
    ends = pd.DatetimeIndex(labelled["label_end"])
    times = pd.DatetimeIndex(quotes["ts_utc"])
    assert ends.equals(times[times.searchsorted(intended, side="left")])
    assert ((ends - intended) <= kind.lookahead(definition, TRADING_DAY).wall).all()
    # ADR 0032: only decisions taken while the market is open are labelled
    assert CLOCK.is_open(decisions).all()
    all_decisions = ns(pd.DatetimeIndex(out.index))
    assert out.loc[~CLOCK.is_open(all_decisions), "value"].isna().all()
    crosses = CLOCK.crosses_close(ns(pd.DatetimeIndex(labelled["label_start"])), ns(ends))
    assert (labelled["crosses_close"].to_numpy() == crosses).all()
    assert labelled["crosses_close"].any()  # decisions near the close hold over the break


def ns(index: pd.DatetimeIndex) -> npt.NDArray[np.int64]:
    values: npt.NDArray[np.int64] = index.as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
    return values


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
