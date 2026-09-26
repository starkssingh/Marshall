"""TGT-002: execution-aware forward returns on hand-computed cases."""

from typing import Any

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import TargetSetConfig, load_config
from xq.core.errors import ConfigError
from xq.targets.base import TargetSpec
from xq.targets.returns import compute, expand, lookahead, sigma_rate

T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")
S = pd.Timedelta(seconds=1)
H = pd.Timedelta(hours=1)
PARAMS: dict[str, Any] = {
    "execution_latency_ms": 1000,
    "max_fill_delay_s": 300,
    "sigma_span_bars": 96,
    "vol_normalized": True,
}

# Quotes: an entry candidate just before and just after t0 + latency, and an exit candidate just
# before and just after t0 + 1h + latency.
QUOTES = pd.DataFrame(
    {
        "ts_utc": [T0 + 0.5 * S, T0 + 2 * S, T0 + H + 0.5 * S, T0 + H + 1.5 * S],
        "bid": [100.0, 101.0, 101.5, 102.0],
        "ask": [100.2, 101.2, 101.7, 102.3],
    }
)


def spec(ref: str, horizon: pd.Timedelta = H, **params: Any) -> TargetSpec:
    return TargetSpec(f"t_{ref}", horizon, ref, {**PARAMS, "normalized": False, **params})  # type: ignore[arg-type]


def sigma(*times: pd.Timestamp, value: float = 0.001) -> pd.Series:
    return pd.Series(value, index=pd.DatetimeIndex(list(times)))


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("long", np.log(102.0 / 101.2)),  # buy at the entry ask, sell at the exit bid
        ("short", np.log(101.0 / 102.3)),  # sell at the entry bid, buy back at the exit ask
        ("mid", np.log((102.0 + 102.3) / (101.0 + 101.2))),
    ],
)
def test_fills_on_the_correct_side_after_the_latency(ref: str, expected: float) -> None:
    out = compute(spec(ref), QUOTES, sigma(T0))
    assert out["value"].iloc[0] == pytest.approx(expected)
    assert out["label_start"].iloc[0] == T0 + 2 * S  # not the 0.5 s quote: before t0 + latency
    assert out["label_end"].iloc[0] == T0 + H + 1.5 * S
    assert np.isnan(out["scale"].iloc[0])


def test_fill_delay_is_the_later_fill_after_its_intended_time() -> None:
    out = compute(spec("long"), QUOTES, sigma(T0, T0 + pd.Timedelta(minutes=15)))
    # entry intended at t0 + 1 s, filled at t0 + 2 s; exit intended at t0 + 1h + 1 s, filled 0.5 s
    # later: the label's delay is the larger one
    assert out["fill_delay_s"].iloc[0] == pytest.approx(1.0)
    assert np.isnan(out["fill_delay_s"].iloc[1])  # no label, no delay


def test_no_label_when_the_fill_comes_too_late_or_never() -> None:
    later = T0 + pd.Timedelta(minutes=15)  # next quote after later + 1 s is 45 minutes away
    out = compute(spec("long"), QUOTES, sigma(T0, later))
    assert np.isfinite(out["value"].iloc[0])
    assert np.isnan(out["value"].iloc[1])
    assert pd.isna(out["label_start"].iloc[1])
    assert pd.isna(out["label_end"].iloc[1])
    beyond = compute(spec("long", pd.Timedelta(hours=4)), QUOTES, sigma(T0))
    assert np.isnan(beyond["value"].iloc[0])  # no quote after the 4-hour exit


def test_zero_latency_fills_at_quotes_exactly_at_the_intended_times() -> None:
    at = T0 + 0.5 * S  # a quote at exactly t, and one at exactly t + 1h
    out = compute(spec("long", execution_latency_ms=0), QUOTES, sigma(at))
    assert out["label_start"].iloc[0] == at
    assert out["label_end"].iloc[0] == at + H
    assert out["value"].iloc[0] == pytest.approx(np.log(101.5 / 100.2))
    assert out["fill_delay_s"].iloc[0] == 0.0


def test_vol_normalized_variant_divides_by_sigma_at_t_scaled_to_the_horizon() -> None:
    raw = compute(spec("long"), QUOTES, sigma(T0))
    normalized = compute(spec("long", normalized=True), QUOTES, sigma(T0, value=0.001))
    scale = 0.001 * np.sqrt(60)
    assert normalized["scale"].iloc[0] == pytest.approx(scale)
    assert normalized["value"].iloc[0] == pytest.approx(raw["value"].iloc[0] / scale)
    zero = compute(spec("long", normalized=True), QUOTES, sigma(T0, value=0.0))
    assert np.isnan(zero["value"].iloc[0])


def test_empty_quotes_give_no_labels() -> None:
    empty = QUOTES.iloc[0:0]
    out = compute(spec("mid"), empty, sigma(T0))
    assert np.isnan(out["value"].iloc[0])


def test_configured_set_expands_to_named_targets() -> None:
    definition = load_config("research", config_dir=REPO / "config").target_set("fwd_returns", "v1")
    names = [s.name for s in expand(definition)]
    assert len(names) == 4 * 3 * 2
    assert "fwd_ret_long_1h" in names
    assert "fwd_ret_short_1d_vol" in names
    assert lookahead(definition) == pd.Timedelta("1d") + S + pd.Timedelta(seconds=300)
    plain = TargetSetConfig(
        kind="forward_return",
        horizons=["1h"],
        price_refs=["mid"],
        params={**PARAMS, "vol_normalized": False},
    )
    assert [s.name for s in expand(plain)] == ["fwd_ret_mid_1h"]


@pytest.mark.parametrize(
    "params",
    [
        {k: v for k, v in PARAMS.items() if k != "execution_latency_ms"},
        {**PARAMS, "max_fill_delay_s": 0},
        {**PARAMS, "unexpected": 1},
    ],
)
def test_params_are_validated(params: dict[str, Any]) -> None:
    definition = TargetSetConfig(
        kind="forward_return", horizons=["1h"], price_refs=["long"], params=params
    )
    with pytest.raises(ConfigError, match="forward_return params"):
        expand(definition)


def test_sigma_rate_is_per_square_root_minute() -> None:
    definition = TargetSetConfig(
        kind="forward_return", horizons=["1h"], price_refs=["long"], params=PARAMS
    )
    index = pd.date_range(T0, periods=5, freq="15min")
    close = pd.Series([100.0, 101.0, 100.0, 101.0, 102.0], index=index)
    rate = sigma_rate(close, definition, pd.Timedelta(minutes=15))
    per_bar = sigma_rate(close, definition, pd.Timedelta(minutes=1))
    np.testing.assert_allclose(rate.to_numpy(), per_bar.to_numpy() / np.sqrt(15))
    assert np.isnan(rate.iloc[0])  # no return yet
