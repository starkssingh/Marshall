"""DS-006 acceptance: the harness catches the five planted leaks from the plan.

Plan, Phase 3: "the harness catches five planted leaks (centered rolling mean, full-sample z-score,
`bfill`, higher-timeframe join on bar start, target shifted into features)". Each leak is paired
with its correct counterpart, which must pass. The leaky constructs are allowed here only because
they live in tests; the lint test bans them from `src/`.
"""

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from xq.datasets import primitives as p
from xq.datasets.asof import asof_join
from xq.datasets.leakage import (
    Check,
    FeatureFn,
    Inputs,
    check_feature_causality,
    check_target_bounds,
    correlation_scan,
)

HORIZON_BARS = 16  # a 4-hour forward return on 15-minute bars


def base(inputs: Inputs) -> pd.DataFrame:
    """Base bars indexed by decision time (each bar's available_at)."""
    frame = inputs["base"]
    return frame.set_index(pd.DatetimeIndex(frame["available_at_utc"], name="decision_time"))


def feature(values: pd.Series, name: str = "f") -> pd.DataFrame:
    return pd.DataFrame({name: values})


def forward_return(inputs: Inputs) -> pd.Series:
    """The target: log return over the next HORIZON_BARS bars (uses the future by design)."""
    close = base(inputs)["close"]
    return np.log(close.shift(-HORIZON_BARS) / close)


# --- the five planted leaks --------------------------------------------------------------------


def centered_rolling_mean(inputs: Inputs) -> pd.DataFrame:
    return feature(base(inputs)["close"].rolling(5, center=True, min_periods=1).mean())


def full_sample_zscore(inputs: Inputs) -> pd.DataFrame:
    close = base(inputs)["close"]
    return feature((close - close.mean()) / close.std())


def bfill_context(inputs: Inputs) -> pd.DataFrame:
    frame = base(inputs)
    h1 = inputs["h1"].set_index(pd.DatetimeIndex(inputs["h1"]["available_at_utc"]))["close"]
    return feature(h1.reindex(h1.index.union(frame.index)).bfill().reindex(frame.index))


def htf_join_on_bar_start(inputs: Inputs) -> pd.DataFrame:
    frame = base(inputs).reset_index()
    h1 = inputs["h1"].rename(columns={"close": "h1_close", "available_at_utc": "h1_available_at"})
    joined = pd.merge_asof(
        frame.sort_values("decision_time"),
        h1[["bar_start_utc", "h1_close", "h1_available_at"]].sort_values("bar_start_utc"),
        left_on="decision_time",
        right_on="bar_start_utc",
        suffixes=("", "_h1"),
    )
    return joined.set_index("decision_time")[["h1_close", "h1_available_at"]]


def target_shifted_into_features(inputs: Inputs) -> pd.DataFrame:
    # "Lagged" by one bar, but the target spans 16 bars, so 15 of them are still in the future.
    return feature(forward_return(inputs).shift(1))


# --- their correct counterparts ----------------------------------------------------------------


def trailing_rolling_mean(inputs: Inputs) -> pd.DataFrame:
    return feature(p.rolling(base(inputs)["close"], 5, min_periods=1))


def expanding_zscore(inputs: Inputs) -> pd.DataFrame:
    close = base(inputs)["close"]
    return feature((close - p.expanding(close)) / p.expanding(close, "std", min_periods=2))


def asof_context(inputs: Inputs) -> pd.DataFrame:
    frame = base(inputs).reset_index()[["decision_time"]]
    joined = asof_join(
        frame, inputs["h1"], on_right="available_at_utc", prefix="h1_", columns=["close"]
    )
    return joined.set_index("decision_time")


def past_return(inputs: Inputs) -> pd.DataFrame:
    return feature(p.log_returns(base(inputs)["close"], HORIZON_BARS))


LEAKS: dict[str, tuple[FeatureFn, set[Check]]] = {
    "centered rolling mean": (centered_rolling_mean, {"truncation", "perturbation"}),
    "full-sample z-score": (full_sample_zscore, {"truncation", "perturbation"}),
    "bfill": (bfill_context, {"truncation", "perturbation"}),
    "higher-timeframe join on bar start": (
        htf_join_on_bar_start,
        {"truncation", "perturbation", "availability"},
    ),
    "target shifted into features": (target_shifted_into_features, {"truncation", "perturbation"}),
}
FIXED: dict[str, FeatureFn] = {
    "centered rolling mean": trailing_rolling_mean,
    "full-sample z-score": expanding_zscore,
    "bfill": asof_context,
    "higher-timeframe join on bar start": asof_context,
    "target shifted into features": past_return,
}


@pytest.mark.parametrize("leak", sorted(LEAKS))
def test_planted_leak_is_caught(leak: str, bar_inputs: dict[str, pd.DataFrame]) -> None:
    fn, expected_checks = LEAKS[leak]
    report = check_feature_causality(fn, bar_inputs, n_points=25, seed=7)
    assert not report.passed, f"{leak} was not caught"
    assert report.checks_failed >= expected_checks, report.summary()


@pytest.mark.parametrize("leak", sorted(FIXED))
def test_correct_counterpart_passes(
    leak: str,
    bar_inputs: dict[str, pd.DataFrame],
    assert_causal: Callable[[FeatureFn, Inputs], None],
) -> None:
    assert_causal(FIXED[leak], bar_inputs)


def test_correlation_scan_flags_the_shifted_target(bar_inputs: dict[str, pd.DataFrame]) -> None:
    targets = pd.DataFrame({"fwd_ret_16": forward_return(bar_inputs)})
    leaky = target_shifted_into_features(bar_inputs)
    report = correlation_scan(leaky, targets)
    assert report.checks_failed == {"correlation"}
    assert "fwd_ret_16" in report.summary()
    assert correlation_scan(past_return(bar_inputs), targets).passed
    # A reviewed pair can be allowed explicitly.
    assert correlation_scan(leaky, targets, allowed={("f", "fwd_ret_16")}).passed


def test_the_first_leaking_instant_is_reported(bar_inputs: dict[str, pd.DataFrame]) -> None:
    report = check_feature_causality(htf_join_on_bar_start, bar_inputs, n_points=25, seed=7)
    audit = [v for v in report.violations if v.check == "availability"]
    assert audit
    assert audit[0].at == base(bar_inputs).index[0]  # the first decision already sees a forming bar


# --- targets are checked against their declared bounds -----------------------------------------

LATENCY = pd.Timedelta(seconds=1)
H = pd.Timedelta(hours=1)


def _ticks(week_ticks: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(week_ticks["ts_utc"], unit="ns", utc=True),
            "bid": week_ticks["bid"],
            "ask": week_ticks["ask"],
        }
    )


def _first_at_or_after(quotes: pd.DataFrame, when: pd.DatetimeIndex) -> np.ndarray:
    ts = quotes["ts_utc"].to_numpy(dtype="datetime64[ns]")
    return np.searchsorted(ts, when.to_numpy(dtype="datetime64[ns]"), side="left")


def honest_target(quotes: pd.DataFrame, sigma: pd.Series) -> pd.DataFrame:
    """Long 1h forward return: buy at the first ask after t+latency, sell at the bid after t+h."""
    t = pd.DatetimeIndex(sigma.index)
    entry = _first_at_or_after(quotes, t + LATENCY)
    exit_ = _first_at_or_after(quotes, t + H + LATENCY)
    ok = exit_ < len(quotes)
    entry_c, exit_c = np.clip(entry, 0, len(quotes) - 1), np.clip(exit_, 0, len(quotes) - 1)
    value = np.log(quotes["bid"].to_numpy()[exit_c] / quotes["ask"].to_numpy()[entry_c])
    return pd.DataFrame(
        {
            "value": np.where(ok, value / sigma.to_numpy(), np.nan),
            "label_start": quotes["ts_utc"].to_numpy()[entry_c],
            "label_end": quotes["ts_utc"].to_numpy()[exit_c],
        },
        index=t,
    )


def understated_label_end(quotes: pd.DataFrame, sigma: pd.Series) -> pd.DataFrame:
    out = honest_target(quotes, sigma)
    return out.assign(label_end=out["label_start"])


def entry_at_signal_close(quotes: pd.DataFrame, sigma: pd.Series) -> pd.DataFrame:
    out = honest_target(quotes, sigma)
    t = pd.DatetimeIndex(sigma.index)
    before = np.clip(_first_at_or_after(quotes, t) - 1, 0, len(quotes) - 1)
    close = ((quotes["bid"] + quotes["ask"]) / 2).to_numpy()[before]
    exit_c = np.clip(_first_at_or_after(quotes, t + H + LATENCY), 0, len(quotes) - 1)
    value = np.log(quotes["bid"].to_numpy()[exit_c] / close) / sigma.to_numpy()
    return out.assign(value=np.where(out["value"].notna(), value, np.nan))


def future_sigma(quotes: pd.DataFrame, sigma: pd.Series) -> pd.DataFrame:
    out = honest_target(quotes, sigma)
    later = sigma.shift(-4).to_numpy()
    return out.assign(value=out["value"] * sigma.to_numpy() / later)


TARGET_LEAKS: dict[str, tuple[Callable[[pd.DataFrame, pd.Series], pd.DataFrame], Check]] = {
    "label_end understated": (understated_label_end, "after_label_end"),
    "entry at the signal bar's close": (entry_at_signal_close, "before_decision"),
    "sigma from the future": (future_sigma, "sigma_other_times"),
}


def _sigma(bar_inputs: dict[str, pd.DataFrame]) -> pd.Series:
    close = base(bar_inputs)["close"]
    sigma = p.ewma_volatility(p.log_returns(close), span=20)
    return sigma.dropna().iloc[:-8]  # leave room for the horizon at the end


def test_honest_target_passes(
    week_ticks: pd.DataFrame, bar_inputs: dict[str, pd.DataFrame]
) -> None:
    report = check_target_bounds(honest_target, _ticks(week_ticks), _sigma(bar_inputs), seed=3)
    assert report.passed, report.summary()
    assert len(report.points) == 25


@pytest.mark.parametrize("leak", sorted(TARGET_LEAKS))
def test_target_leak_is_caught(
    leak: str, week_ticks: pd.DataFrame, bar_inputs: dict[str, pd.DataFrame]
) -> None:
    fn, check = TARGET_LEAKS[leak]
    report = check_target_bounds(fn, _ticks(week_ticks), _sigma(bar_inputs), seed=3)
    assert check in report.checks_failed, report.summary()


def test_label_bounds_are_checked(
    week_ticks: pd.DataFrame, bar_inputs: dict[str, pd.DataFrame]
) -> None:
    def backwards(quotes: pd.DataFrame, sigma: pd.Series) -> pd.DataFrame:
        out = honest_target(quotes, sigma)
        return out.assign(label_start=out.index - H)

    report = check_target_bounds(backwards, _ticks(week_ticks), _sigma(bar_inputs), seed=3)
    assert "label_bounds" in report.checks_failed
