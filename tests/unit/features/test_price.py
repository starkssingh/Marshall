"""FEAT-002: price-structure features on hand-computed cases."""

import numpy as np
import pandas as pd
import pytest

from helpers.features import hand_bars, run
from xq.features.base import bar_sigma
from xq.features.price import (
    candle,
    ema_distance,
    extreme_distance,
    gap,
    log_return,
    range_sigma,
    session_vwap,
)


def ewma_sigma(returns: list[float], span: int) -> float:
    """Sigma-hat after `returns` by hand: zero-mean EWMA, bias-adjusted weights (1 - a)^i."""
    a = 2 / (span + 1)
    weights = [(1 - a) ** i for i in range(len(returns))][::-1]
    return float(
        np.sqrt(sum(w * r * r for w, r in zip(weights, returns, strict=True)) / sum(weights))
    )


def test_log_returns_over_n_bars() -> None:
    out = run(log_return, hand_bars([100.0, 110.0, 121.0]), bars=2)
    assert out["value"].iloc[:2].isna().all()
    assert out["value"].iloc[2] == pytest.approx(np.log(1.21))
    assert out.index.equals(pd.DatetimeIndex(hand_bars([1.0, 1.0, 1.0])["available_at_utc"]))


def test_candle_ratios_and_a_bar_without_range() -> None:
    bars = hand_bars([12.0, 5.0], open_=[10.0, 5.0], high=[14.0, 5.0], low=[8.0, 5.0])
    out = run(candle, bars)
    assert out.iloc[0].tolist() == pytest.approx([2 / 6, 2 / 6, 2 / 6])  # body, upper, lower
    assert out.iloc[1].isna().all()
    falling = run(candle, hand_bars([9.0], open_=[13.0], high=[14.0], low=[8.0]))
    assert falling.iloc[0].tolist() == pytest.approx([-4 / 6, 1 / 6, 1 / 6])


def test_range_in_sigma_units() -> None:
    close = [100.0, 101.0, 100.5, 102.0]
    bars = hand_bars(close, high=[100.5, 101.5, 101.5, 103.0], low=[99.5, 100.0, 100.0, 101.0])
    out = run(range_sigma, bars, sigma_span=2)
    r = list(np.diff(np.log(close)))
    assert out["value"].iloc[:2].isna().all()  # two returns needed
    assert out["value"].iloc[2] == pytest.approx(np.log(101.5 / 100.0) / ewma_sigma(r[:2], 2))
    assert out["value"].iloc[3] == pytest.approx(np.log(103.0 / 101.0) / ewma_sigma(r, 2))


def test_gap_after_the_daily_break_and_zero_on_contiguous_bars() -> None:
    # 20:15, 20:30, 20:45 (to the 21:00 UTC close in March, DST) and the reopen bar at 22:00
    starts = ["2024-03-12 20:15", "2024-03-12 20:30", "2024-03-12 20:45", "2024-03-12 22:00"]
    close = [100.0, 101.0, 100.0, 104.0]
    bars = hand_bars(close, open_=[100.0, 100.0, 101.0, 103.0], starts=starts)
    out = run(gap, bars, sigma_span=2)["value"]
    sigma_before = ewma_sigma(list(np.diff(np.log(close[:3]))), 2)
    assert out.iloc[:3].isna().all()  # sigma of the previous bar unknown until two returns
    assert out.iloc[3] == pytest.approx(np.log(103.0 / 100.0) / sigma_before)
    contiguous = hand_bars([100.0, 101.0, 100.0, 104.0, 104.5], open_=[100, 100, 101, 103, 104])
    assert run(gap, contiguous, sigma_span=2)["value"].iloc[3:].tolist() == [0.0, 0.0]


def test_distance_to_the_rolling_extremes_and_the_position_in_the_range() -> None:
    close = [100.0, 102.0, 101.0, 103.0]
    high, low = [101.0, 104.0, 102.0, 103.5], [99.0, 101.0, 100.0, 102.0]
    out = run(extreme_distance, hand_bars(close, high=high, low=low), window=3, sigma_span=2)
    sigma = ewma_sigma(list(np.diff(np.log(close))), 2)
    last = out.iloc[3]
    assert last["to_max"] == pytest.approx(np.log(103.0 / 104.0) / sigma)
    assert last["to_min"] == pytest.approx(np.log(103.0 / 100.0) / sigma)
    assert last["position"] == pytest.approx((103.0 - 100.0) / (104.0 - 100.0))
    assert out["position"].iloc[:2].isna().all()  # three bars needed


def test_session_vwap_is_tick_weighted_and_resets_with_the_trading_day() -> None:
    starts = ["2024-03-12 20:15", "2024-03-12 20:30", "2024-03-12 20:45", "2024-03-12 22:00"]
    close = [100.0, 102.0, 101.0, 105.0]
    high, low = [101.0, 103.0, 102.0, 106.0], [99.0, 100.0, 100.0, 103.0]
    bars = hand_bars(close, high=high, low=low, ticks=[1, 2, 1, 5], starts=starts)
    value = run(session_vwap, bars, sigma_span=2)["value"]
    sigma = bar_sigma(bars, 2).to_numpy()
    typical = (np.array(high) + np.array(low) + np.array(close)) / 3
    vwap_day = (typical[0] * 1 + typical[1] * 2 + typical[2] * 1) / 4
    assert value.iloc[2] * sigma[2] == pytest.approx(np.log(101.0 / vwap_day))
    assert value.iloc[3] * sigma[3] == pytest.approx(np.log(105.0 / typical[3]))  # a new day


def test_distance_to_the_ema() -> None:
    close = [100.0, 101.0, 103.0, 102.0]
    value = run(ema_distance, hand_bars(close), span=3, sigma_span=2)["value"]
    a = 2 / (3 + 1)
    weights = np.array([(1 - a) ** i for i in range(4)])[::-1]
    ema = float(np.dot(weights, close) / weights.sum())
    sigma = ewma_sigma(list(np.diff(np.log(close))), 2)
    assert value.iloc[:2].isna().all()  # the EMA needs three closes
    assert value.iloc[3] == pytest.approx(np.log(102.0 / ema) / sigma)
