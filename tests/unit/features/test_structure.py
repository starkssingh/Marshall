"""FEAT-005: market-structure features on hand cases; swing points appear only after their
confirmation lag."""

import numpy as np
import pandas as pd
import pytest

from helpers.features import hand_bars, run
from xq.core.errors import ConfigError
from xq.features.base import bar_sigma
from xq.features.structure import (
    adx,
    breakout,
    compression,
    efficiency_ratio,
    mean_reversion_z,
    prior_day,
    prior_session,
    round_distance,
    swing,
)

#: A swing high at bar 2 (high 5) and a swing low at bar 6 (low 0.5), strength 2.
HIGH = [1.0, 2.0, 5.0, 3.0, 2.0, 1.5, 1.2, 1.4, 1.6, 2.0]
LOW = [0.8, 1.5, 4.0, 2.5, 1.5, 1.0, 0.5, 1.0, 1.2, 1.5]
CLOSE = [0.9, 1.8, 4.5, 2.8, 1.8, 1.2, 0.9, 1.2, 1.5, 1.8]


def swing_bars(n: int = len(CLOSE)) -> pd.DataFrame:
    return hand_bars(CLOSE[:n], open_=CLOSE[:n], high=HIGH[:n], low=LOW[:n])


def test_a_swing_is_used_only_after_its_confirmation_lag() -> None:
    out = run(swing, swing_bars(), strength=2, sigma_span=2)
    # the swing high formed on bar 2 is confirmed on bar 4, two bars later
    assert out["high_age"].iloc[:4].isna().all()
    assert out["high_age"].iloc[4:].tolist() == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    sigma = bar_sigma(swing_bars(), 2)
    assert out["high"].iloc[4] == pytest.approx(np.log(1.8 / 5.0) / sigma.iloc[4])
    # the swing low of bar 6 (low 0.5) is confirmed on bar 8
    assert out["low_age"].iloc[:8].isna().all()
    assert out["low_age"].iloc[8:].tolist() == [2.0, 3.0]
    assert out["low"].iloc[8] == pytest.approx(np.log(1.5 / 0.5) / sigma.iloc[8])
    # computed on the bars up to the swing's own bar, or before its confirmation, it is unknown
    for n in (3, 4):
        early = run(swing, swing_bars(n), strength=2, sigma_span=2)
        assert early["high_age"].isna().all()
    assert run(swing, swing_bars(5), strength=2, sigma_span=2)["high_age"].iloc[4] == 2.0


def test_a_tie_is_no_swing_and_a_newer_swing_replaces_an_older_one() -> None:
    flat = hand_bars([1.0] * 7, high=[2.0, 2.0, 3.0, 3.0, 2.0, 2.0, 2.0], low=[0.5] * 7)
    assert run(swing, flat, strength=1, sigma_span=2)["high_age"].isna().all()
    high = [1.0, 3.0, 1.0, 1.0, 4.0, 1.0, 1.0]
    bars = hand_bars([1.0] * 7, high=high, low=[0.5] * 7)
    age = run(swing, bars, strength=1, sigma_span=2)["high_age"]
    assert age.iloc[2:].tolist() == [1.0, 2.0, 3.0, 1.0, 2.0]  # bar 1's swing, then bar 4's


def test_wilder_adx_by_hand() -> None:
    bars = hand_bars(
        [9.0, 10.5, 9.0, 11.5, 12.0],
        open_=[9.0, 9.5, 10.0, 9.5, 11.5],
        high=[10.0, 11.0, 10.5, 12.0, 12.5],
        low=[8.0, 9.0, 8.5, 9.5, 11.0],
    )
    out = run(adx, bars, window=2)
    # +DM 1, 0, 1.5, 0.5; -DM 0, 0.5, 0, 0; true ranges 2, 2, 3, 1.5 (from bar 1)
    tr, plus, minus = 2.0, 0.5, 0.25  # seeded: means of bars 1 and 2
    di = [(100 * plus / tr, 100 * minus / tr)]
    for t, p, m in ((3.0, 1.5, 0.0), (1.5, 0.5, 0.0)):
        tr, plus, minus = tr + (t - tr) / 2, plus + (p - plus) / 2, minus + (m - minus) / 2
        di.append((100 * plus / tr, 100 * minus / tr))
    dx = [100 * abs(p - m) / (p + m) for p, m in di]
    first = (dx[0] + dx[1]) / 2
    assert out["plus_di"].iloc[2:].tolist() == pytest.approx([p for p, _ in di])
    assert out["minus_di"].iloc[2:].tolist() == pytest.approx([m for _, m in di])
    assert out["adx"].iloc[:3].isna().all()  # from bar 2 * window - 1
    assert out["adx"].iloc[3:].tolist() == pytest.approx([first, first + (dx[2] - first) / 2])
    assert di[0] == pytest.approx((25.0, 12.5))
    assert first == pytest.approx((100 / 3 + 700 / 9) / 2)


def test_breakout_efficiency_z_score_and_compression() -> None:
    close = [10.0, 11.0, 10.5, 12.0, 11.0]
    high, low = [10.5, 11.5, 11.0, 12.5, 12.0], [9.5, 10.0, 10.0, 10.5, 10.8]
    bars = hand_bars(close, high=high, low=low)
    out = run(breakout, bars, window=2, sigma_span=2)
    sigma = bar_sigma(bars, 2)
    assert out["up"].iloc[3] == pytest.approx(np.log(12.0 / 11.5) / sigma.iloc[3])  # broke out
    assert out["down"].iloc[4] == pytest.approx(np.log(11.0 / 10.0) / sigma.iloc[4])
    ratio = run(efficiency_ratio, bars, window=3)["value"]
    assert ratio.iloc[3] == pytest.approx(2.0 / (1.0 + 0.5 + 1.5))
    z = run(mean_reversion_z, bars, window=3)["value"]
    window = np.array(close[2:5])
    assert z.iloc[4] == pytest.approx((11.0 - window.mean()) / window.std(ddof=1))
    ranks = run(compression, bars, window=2, history=3)["value"]
    spreads = [np.log(max(high[k - 1 : k + 1]) / min(low[k - 1 : k + 1])) for k in (2, 3, 4)]
    assert ranks.iloc[:3].isna().all()
    assert ranks.iloc[4] == pytest.approx(np.mean(np.array(spreads) <= spreads[-1]))


def test_prior_day_levels_apply_from_the_next_trading_day() -> None:
    # two bars of trading day 2024-03-12 (before 21:00 UTC) and two of 2024-03-13 (after)
    starts = ["2024-03-12 20:15", "2024-03-12 20:30", "2024-03-12 22:00", "2024-03-12 22:15"]
    close = [100.0, 101.0, 102.0, 99.0]
    bars = hand_bars(close, high=[100.5, 103.0, 102.5, 102.0], low=[99.0, 100.0, 101.0, 98.5],
                     starts=starts, open_=close)  # fmt: skip
    out = run(prior_day, bars, sigma_span=2)
    sigma = bar_sigma(bars, 2)
    assert out.iloc[:2].isna().all().all()  # no earlier trading day
    assert out["high"].iloc[2] == pytest.approx(np.log(102.0 / 103.0) / sigma.iloc[2])
    assert out["low"].iloc[3] == pytest.approx(np.log(99.0 / 99.0) / sigma.iloc[3])


def test_prior_session_uses_only_a_completed_occurrence() -> None:
    # London opens at 08:00 UTC in March (GMT): bars from 07:30 to 09:00 UTC on 2024-03-12
    starts = [f"2024-03-12 {h}" for h in ("07:30", "07:45", "08:00", "08:15", "08:30", "08:45")]
    close = [100.0, 100.5, 101.0, 102.0, 101.5, 101.0]
    bars = hand_bars(close, open_=close, high=[c + 0.5 for c in close],
                     low=[c - 0.5 for c in close], starts=starts)  # fmt: skip
    out = run(prior_session, bars, session="london", sigma_span=2)
    assert out.isna().all().all()  # London is still running: no completed occurrence yet
    # the next day's 07:30 bar sees the whole of yesterday's London session (08:00 onwards)
    later = hand_bars(
        [*close, 103.0],
        open_=[*close, 103.0],
        high=[c + 0.5 for c in [*close, 103.0]],
        low=[c - 0.5 for c in [*close, 103.0]],
        starts=[*starts, "2024-03-13 07:30"],
    )
    value = run(prior_session, later, session="london", sigma_span=2)
    sigma = bar_sigma(later, 2)
    assert value["high"].iloc[6] == pytest.approx(np.log(103.0 / 102.5) / sigma.iloc[6])
    assert value["low"].iloc[6] == pytest.approx(np.log(103.0 / 100.5) / sigma.iloc[6])
    with pytest.raises(ConfigError, match="no session"):
        run(prior_session, bars, session="sydney", sigma_span=2)


def test_distance_to_round_numbers() -> None:
    close = [1996.0, 2003.0, 2012.0]
    bars = hand_bars(close)
    value = run(round_distance, bars, step=10.0, sigma_span=2)["value"]
    sigma = bar_sigma(bars, 2)
    assert value.iloc[2] == pytest.approx(np.log(2012.0 / 2010.0) / sigma.iloc[2])
    fifty = run(round_distance, bars, step=50.0, sigma_span=2)["value"]
    assert fifty.iloc[2] == pytest.approx(np.log(2012.0 / 2000.0) / sigma.iloc[2])
