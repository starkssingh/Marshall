"""ROB-005: noise injection — price noise scales with the spread, feature noise with each
feature's causal standard deviation and never reads later rows; an edge living at the scale of
the spread (a bid-ask bounce) collapses under spread-sized noise, while a genuine trend edge
degrades smoothly under price and feature noise."""

import numpy as np
import pandas as pd
import pytest

from helpers.quality import repo_config
from helpers.strategies import drift_returns
from xq.robustness.noise import noise_curve, noisy_features, noisy_prices

SETTINGS = repo_config().validation_config().noise


def test_price_noise_is_a_fraction_of_the_spread() -> None:
    prices = pd.Series(2000.0, index=range(20_000))
    spread = pd.Series(np.where(np.arange(20_000) < 10_000, 0.2, 0.6), index=prices.index)
    noisy = noisy_prices(prices, spread, 0.5, seed=1) - prices
    assert noisy[:10_000].std() == pytest.approx(0.1, rel=0.03)
    assert noisy[10_000:].std() == pytest.approx(0.3, rel=0.03)
    pd.testing.assert_series_equal(noisy_prices(prices, spread, 0.0, seed=1), prices)
    frame = pd.DataFrame({"open": prices, "close": prices})
    both = noisy_prices(frame, 0.2, 1.0, seed=2) - frame
    assert abs(np.corrcoef(both["open"], both["close"])[0, 1]) < 0.05  # independent columns
    with pytest.raises(ValueError, match="spread"):
        noisy_prices(prices, spread.iloc[:10], 0.5, seed=1)


def test_feature_noise_is_causal_and_scaled_by_the_feature_sigma() -> None:
    rng = np.random.default_rng(3)
    features = pd.DataFrame({"a": rng.normal(0, 2.0, 5000), "b": rng.normal(0, 0.01, 5000)})
    noise = noisy_features(features, 0.5, seed=4) - features
    assert (noise.iloc[:20] == 0).all().all()  # no sigma known yet: left as they are
    assert noise["a"].iloc[100:].std() == pytest.approx(1.0, rel=0.05)
    assert noise["b"].iloc[100:].std() == pytest.approx(0.005, rel=0.05)
    changed = features.copy()
    changed.iloc[3000:] *= 50  # later rows change ...
    again = noisy_features(changed, 0.5, seed=4) - changed
    pd.testing.assert_frame_equal(
        again.iloc[:3001], noise.iloc[:3001]
    )  # ... earlier noise does not
    with pytest.raises(ValueError, match="not be negative"):
        noisy_features(features, -0.1, seed=4)


def bounce_market(n: int, spread: float, seed: int) -> np.ndarray:
    """Mids that bounce between bid and ask around a slowly moving efficient price."""
    rng = np.random.default_rng(seed)
    efficient = np.cumsum(rng.normal(0.0, 0.2 * spread, n))
    return 2000.0 + efficient + spread / 2 * rng.choice([-1.0, 1.0], n)


def test_an_edge_at_the_scale_of_the_spread_collapses_under_spread_sized_noise() -> None:
    spread = 0.3
    mids = bounce_market(20_000, spread, seed=5)
    moves = np.diff(mids, prepend=mids[0])

    def evaluate(level: float, seed: int) -> np.ndarray:
        seen = noisy_prices(pd.Series(mids), spread, level, seed).to_numpy()
        signal = -np.sign(np.diff(seen, prepend=seen[0]))  # fade the last move
        position = np.roll(signal, 1)
        position[0] = 0.0
        return position * moves  # P&L on the true mids

    curve = noise_curve(
        evaluate,
        kind="price",
        levels=SETTINGS.price_levels,
        n_seeds=SETTINGS.n_seeds,
        seed=6,
        periods_per_year=252,
    )
    assert curve.base > 3  # the bounce is (gross) money for nothing ...
    table = curve.table()
    assert table.loc[1.0, "retention"] < 0.5  # ... until the inputs are off by a spread
    assert table.loc[5.0, "retention"] < 0.15
    assert (table["q95"].iloc[1:] < curve.base).all()  # no draw keeps the undisturbed edge


def test_a_genuine_trend_edge_degrades_smoothly() -> None:
    returns = drift_returns(2500, seed=7, drift_sigma=0.0004)
    prices = pd.Series(2000.0 * np.exp(np.cumsum(returns)))
    spread = 0.3

    def positions(seen: pd.Series, deadband: float = 0.5) -> np.ndarray:
        r = np.log(seen).diff()
        window = r.rolling(40)
        t_stat = (window.mean() / (window.std() / np.sqrt(40))).shift(1)
        return np.where(np.abs(t_stat) > deadband, np.sign(t_stat), 0.0)

    def price_noise(level: float, seed: int) -> np.ndarray:
        seen = noisy_prices(prices, spread, level, seed)
        return positions(seen) * returns

    def feature_noise(level: float, seed: int) -> np.ndarray:
        r = np.log(prices).diff()
        window = r.rolling(40)
        t_stat = (window.mean() / (window.std() / np.sqrt(40))).shift(1)
        noisy = noisy_features(t_stat.to_frame("t"), level, seed)["t"]
        return np.where(np.abs(noisy) > 0.5, np.sign(noisy), 0.0) * returns

    common = {"n_seeds": SETTINGS.n_seeds, "seed": 8, "periods_per_year": 252}
    by_price = noise_curve(price_noise, kind="price", levels=SETTINGS.price_levels, **common)
    assert by_price.base > 0.5
    assert by_price.table()["retention"].min() > 0.8  # spread-sized noise is nothing to a trend
    assert np.isnan(by_price.breakdown_level)
    by_feature = noise_curve(
        feature_noise, kind="feature", levels=SETTINGS.feature_levels, **common
    )
    medians = by_feature.median()
    assert np.all(np.diff(medians) > -0.3 * by_feature.base)  # smooth: no cliff between levels
    assert by_feature.table().loc[0.25, "retention"] > 0.7
    assert by_feature.table().loc[1.0, "retention"] > 0.3  # still an edge at a full sigma
