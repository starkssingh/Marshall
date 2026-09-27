"""VOL-001: range estimators and Wilder's ATR match hand computations, recover the variance of a
simulated Brownian path (Yang-Zhang including opening gaps, the others without) and are
trailing."""

import math

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.core.config import EstimatorsConfig, load_config
from xq.research.volatility.estimators import (
    close_to_close,
    garman_klass,
    parkinson,
    range_estimators,
    rogers_satchell,
    wilder_atr,
    yang_zhang,
)

BARS = pd.DataFrame(
    {
        "open": [100.0, 101.0, 102.5, 101.5],
        "high": [102.0, 103.0, 103.0, 102.0],
        "low": [99.0, 100.5, 101.0, 100.0],
        "close": [101.5, 102.0, 101.2, 101.8],
    }
)


def test_estimators_match_hand_computations() -> None:
    o, h, lo, c = (BARS[k].to_numpy() for k in ("open", "high", "low", "close"))
    ln = np.log
    # Parkinson, window 2, at the last bar
    expected = (ln(h[2] / lo[2]) ** 2 + ln(h[3] / lo[3]) ** 2) / 2 / (4 * math.log(2))
    assert parkinson(BARS, 2)[3] == pytest.approx(expected)
    assert np.isnan(parkinson(BARS, 2)[0])
    # Garman-Klass
    gk = [
        0.5 * ln(h[i] / lo[i]) ** 2 - (2 * math.log(2) - 1) * ln(c[i] / o[i]) ** 2 for i in (2, 3)
    ]
    assert garman_klass(BARS, 2)[3] == pytest.approx(np.mean(gk))
    # Rogers-Satchell
    rs = [
        ln(h[i] / c[i]) * ln(h[i] / o[i]) + ln(lo[i] / c[i]) * ln(lo[i] / o[i]) for i in (1, 2, 3)
    ]
    assert rogers_satchell(BARS, 3)[3] == pytest.approx(np.mean(rs))
    # close-to-close: sample variance of the returns in the window
    r = [ln(c[i] / c[i - 1]) for i in (1, 2, 3)]
    assert close_to_close(BARS, 3)[3] == pytest.approx(np.var(r, ddof=1))
    assert np.isnan(close_to_close(BARS, 3)[2])  # only two returns exist at the third bar
    # Yang-Zhang, window 2: var(opening) + k var(body) + (1 - k) RS
    opening = [ln(o[i] / c[i - 1]) for i in (2, 3)]
    body = [ln(c[i] / o[i]) for i in (2, 3)]
    k = 0.34 / (1.34 + 3 / 1)
    expected = np.var(opening, ddof=1) + k * np.var(body, ddof=1) + (1 - k) * np.mean(rs[1:])
    assert yang_zhang(BARS, 2)[3] == pytest.approx(expected)


def test_wilder_atr_by_hand() -> None:
    tr = [3.0, max(2.5, 1.5, 1.0), max(2.0, 1.0, 1.0), max(2.0, 0.8, 1.2)]
    atr = wilder_atr(BARS, 2)
    assert np.isnan(atr[0])
    assert atr[1] == pytest.approx((tr[0] + tr[1]) / 2)
    assert atr[2] == pytest.approx(atr[1] + (tr[2] - atr[1]) / 2)
    assert atr[3] == pytest.approx(atr[2] + (tr[3] - atr[2]) / 2)


def _brownian_bars(n: int, steps: int, sigma: float, gap_sigma: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    increments = rng.standard_normal((n, steps)) * sigma / math.sqrt(steps)
    gaps = rng.standard_normal(n) * gap_sigma
    rows = []
    level = 0.0
    for i in range(n):
        start = level + gaps[i]
        path = start + np.concatenate([[0.0], np.cumsum(increments[i])])
        rows.append((start, path.max(), path.min(), path[-1]))
        level = path[-1]
    log = np.array(rows)
    return pd.DataFrame(2000.0 * np.exp(log), columns=["open", "high", "low", "close"])


def test_estimators_recover_the_variance_of_a_brownian_path() -> None:
    # 4,000 steps per bar: a discretely monitored range is slightly short (a bias of about 2 %)
    sigma, gap = 0.01, 0.005
    bars = _brownian_bars(2000, 4000, sigma, gap, seed=3)
    n = len(bars)
    intraday, total = sigma**2, sigma**2 + gap**2
    for estimator in (parkinson, garman_klass, rogers_satchell):
        assert estimator(bars, n)[-1] == pytest.approx(intraday, rel=0.08)
    # the n - 1 returns of n bars: the first bar has no previous close
    assert np.isnan(yang_zhang(bars, n)[-1])
    assert yang_zhang(bars, n - 1)[-1] == pytest.approx(total, rel=0.08)
    assert close_to_close(bars, n - 1)[-1] == pytest.approx(total, rel=0.08)
    assert rogers_satchell(bars, n)[-1] < 0.9 * yang_zhang(bars, n - 1)[-1]  # gaps excluded


def test_estimators_are_trailing() -> None:
    bars = _brownian_bars(200, 50, 0.01, 0.002, seed=1)
    cfg = EstimatorsConfig(window=20, atr_window=14)
    base = range_estimators(bars, cfg)
    changed = bars.copy()
    changed.iloc[120:] *= 1.5
    after = range_estimators(changed, cfg)
    pd.testing.assert_frame_equal(base.iloc[:120], after.iloc[:120])
    assert not np.allclose(base["yang_zhang"].iloc[121:], after["yang_zhang"].iloc[121:])
    assert base.iloc[:19].drop(columns=["atr", "atr_rel"]).isna().all().all()


def test_the_table_and_the_price_basis() -> None:
    cfg = load_config("dev", config_dir=REPO / "config").volatility_config().estimators
    mid = BARS.add_prefix("mid_")
    table = range_estimators(mid, EstimatorsConfig(window=2, atr_window=2), prefix="mid_")
    assert list(table.columns) == [
        "close_to_close",
        "parkinson",
        "garman_klass",
        "rogers_satchell",
        "yang_zhang",
        "atr",
        "atr_rel",
    ]
    assert table["atr_rel"].iloc[-1] == pytest.approx(table["atr"].iloc[-1] / 101.8)
    assert cfg.window == 20
    with pytest.raises(KeyError, match="price columns"):
        range_estimators(BARS, cfg, prefix="bid_")
    bad = BARS.copy()
    bad.loc[0, "high"] = 99.0
    with pytest.raises(ValueError, match="low <= open"):
        parkinson(bad, 2)
