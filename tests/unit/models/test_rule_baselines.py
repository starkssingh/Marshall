"""BASE-002: rule baselines give known positions and trades on hand-built series; the random-entry
null keeps the template's trade count, holding times and sides."""

import math
from collections import Counter
from datetime import date

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from xq.backtest.costs import CostModel
from xq.backtest.vectorized import run_vectorized
from xq.core.config import load_config
from xq.core.errors import ConfigError
from xq.core.seeds import derive_seed
from xq.data.calendar import MarketClock
from xq.models.baselines import (
    RuleStrategyConfig,
    VolTargetConfig,
    donchian_breakout,
    holding_episodes,
    ma_crossover,
    positions_at,
    random_entry,
    random_entry_null,
    rule_exposure,
    signal_bars,
    time_series_momentum,
    zscore_reversion,
)

CFG = load_config("research", config_dir=REPO / "config")


def bars(*closes: float, spread: float = 1.0) -> pd.DataFrame:
    close = pd.Series(
        closes, index=pd.date_range("2024-01-01 22:00", periods=len(closes), freq="D", tz="UTC")
    )
    close.index.name = "available_at"
    half = spread / 2
    return pd.DataFrame({"open": close, "high": close + half, "low": close - half, "close": close})


def test_signal_bars_are_the_distinct_context_bars() -> None:
    decisions = pd.date_range("2024-01-02 00:00", periods=6, freq="8h", tz="UTC")
    available = pd.to_datetime(
        ["2024-01-01 22:00"] * 3 + ["2024-01-02 22:00"] * 3, utc=True
    ).to_series(index=decisions)
    features = pd.DataFrame(
        {
            "ctx_1d_available_at": available,
            "ctx_1d_open": [1.0, 1.0, 1.0, 2.0, 2.0, 2.0],
            "ctx_1d_high": [1.5] * 3 + [2.5] * 3,
            "ctx_1d_low": [0.5] * 3 + [1.5] * 3,
            "ctx_1d_close": [1.2] * 3 + [2.2] * 3,
        },
        index=decisions,
    )
    daily = signal_bars(features, "ctx_1d_")
    expected = pd.to_datetime(["2024-01-01 22:00", "2024-01-02 22:00"], utc=True)
    assert daily.index.tolist() == expected.tolist()
    assert daily["close"].tolist() == [1.2, 2.2]
    with pytest.raises(KeyError, match="signal-bar columns"):
        signal_bars(features, "ctx_4h_")


def test_time_series_momentum_is_the_sign_of_the_lookback_return() -> None:
    signal = time_series_momentum(bars(100, 101, 102, 101, 99, 98), {"lookback": 2})
    assert signal.tolist() == [0.0, 0.0, 1.0, 0.0, -1.0, -1.0]


def test_ma_crossover_follows_the_fast_average() -> None:
    # fast (2): -, 10.5, 11.5, 11.5, 10, 8.5; slow (3): -, -, 11, 11.33, 10.67, 9.33
    signal = ma_crossover(bars(10, 11, 12, 11, 9, 8), {"fast": 2, "slow": 3})
    assert signal.tolist() == [0.0, 0.0, 1.0, 1.0, -1.0, -1.0]
    with pytest.raises(ConfigError, match="fast < slow"):
        ma_crossover(bars(1, 2, 3), {"fast": 3, "slow": 2})


def test_zscore_reversion_fades_the_spike_until_the_mean() -> None:
    frame = bars(100, 100, 101, 99, 100, 110, 104, 101, 99, 100)
    # z over 5 bars: -, -, -, -, 0.0, 1.77, 0.27, -0.41, -0.86, -0.63
    signal = zscore_reversion(frame, {"lookback": 5, "entry": 1.5, "exit": 0.0})
    assert signal.tolist() == [0, 0, 0, 0, 0, -1, -1, 0, 0, 0]
    with pytest.raises(ConfigError, match="exit < entry"):
        zscore_reversion(frame, {"lookback": 5, "entry": 1.0, "exit": 1.0})


def test_donchian_breakout_channel_exit_and_reversal() -> None:
    frame = bars(100, 100.5, 100, 100.5, 100, 103, 104, 103.5, 101, 100, 99, 96, 95, 99)
    signal = donchian_breakout(frame, {"entry": 3, "exit": 2, "atr_window": 2, "atr_stop": 2.0})
    # bar 5 breaks the 3-bar high (101); bar 8 falls below the 2-bar exit low (103) and the 3-bar
    # low (102.5): exit and short; bar 13 rises above the 2-bar exit high (96.5): flat.
    assert signal.tolist() == [0, 0, 0, 0, 0, 1, 1, 1, -1, -1, -1, -1, -1, 0]


def test_donchian_atr_stop_is_fixed_at_entry() -> None:
    # Long at bar 5 (close 103) with ATR 2.25 and a 0.5-ATR stop: 101.875. The 20-bar exit channel
    # is still unknown, so only the stop closes the trade, at bar 6 (close 101.5); no reversal,
    # since 101.5 is above the 3-bar low (99.5).
    frame = bars(100, 100.5, 100, 100.5, 100, 103, 101.5, 101, 101.2, 101)
    signal = donchian_breakout(frame, {"entry": 3, "exit": 20, "atr_window": 2, "atr_stop": 0.5})
    assert signal.tolist() == [0, 0, 0, 0, 0, 1, 0, 0, 0, 0]


def test_vol_targeting_scales_to_the_target_and_caps() -> None:
    # log returns alternate +a, -a: the 4-bar standard deviation is a * sqrt(4/3) for ddof 1
    a = 0.01
    closes = 100 * np.exp(np.cumsum([0.0] + [a if i % 2 == 0 else -a for i in range(9)]))
    frame = bars(*closes)
    config = VolTargetConfig(annual_vol=0.10, lookback=4, max_exposure=2.0)
    exposure = rule_exposure(
        frame,
        RuleStrategyConfig(rule="buy_and_hold", vol_target=True),
        vol_target=config,
        periods_per_year=252,
    )
    realized = a * math.sqrt(4 / 3) * math.sqrt(252)
    assert exposure.iloc[:4].tolist() == [0.0] * 4  # unknown volatility: no position
    np.testing.assert_allclose(exposure.iloc[4:], 0.10 / realized)
    capped = VolTargetConfig(annual_vol=10.0, lookback=4, max_exposure=2.0)
    strategy = RuleStrategyConfig(rule="buy_and_hold", vol_target=True)
    assert rule_exposure(frame, strategy, vol_target=capped).iloc[-1] == 2.0


def test_rule_configuration_errors() -> None:
    frame = bars(1, 2, 3)
    with pytest.raises(ConfigError, match="unknown rule"):
        rule_exposure(frame, RuleStrategyConfig(rule="astrology"))
    with pytest.raises(ConfigError, match="no target is set"):
        rule_exposure(frame, RuleStrategyConfig(rule="buy_and_hold", vol_target=True))
    with pytest.raises(ConfigError, match="'lookback' is missing"):
        rule_exposure(frame, RuleStrategyConfig(rule="time_series_momentum"))


def test_positions_follow_the_latest_available_signal_bar() -> None:
    exposure = pd.Series(
        [1.0, -1.0], index=pd.to_datetime(["2024-01-01 22:00", "2024-01-02 22:00"], utc=True)
    )
    decisions = pd.DatetimeIndex(
        pd.to_datetime(
            ["2024-01-01 21:45", "2024-01-01 22:00", "2024-01-02 21:45", "2024-01-02 22:00"],
            utc=True,
        )
    )
    assert positions_at(decisions, exposure).tolist() == [0.0, 1.0, 1.0, -1.0]


TEMPLATE = pd.Series(
    [0, 1, 1, 1, 0, 0, -1, -1, 0, 0, 0, 0.5, 0.7, 0, 0, 0, 0, 0, 0, 0],
    index=pd.date_range("2024-03-12 10:00", periods=20, freq="15min", tz="UTC"),
    dtype=float,
)


def episode_profile(positions: pd.Series) -> Counter[tuple[float, ...]]:
    values = positions.to_numpy()
    return Counter(tuple(values[s : s + n]) for s, n in holding_episodes(positions))


def test_holding_episodes() -> None:
    assert holding_episodes(TEMPLATE) == [(1, 3), (6, 2), (11, 2)]
    flip = pd.Series([1.0, 1.0, -1.0, 0.0, 1.0])
    assert holding_episodes(flip) == [(0, 2), (2, 1), (4, 1)]


def test_random_entry_matches_trade_count_holding_times_and_sides() -> None:
    draws = list(random_entry_null(TEMPLATE, 200, seed=11))
    for draw in draws:
        assert draw.index.equals(TEMPLATE.index)
        assert episode_profile(draw) == episode_profile(TEMPLATE)
    assert len({tuple(d) for d in draws}) > 150  # the placements really vary
    assert draws[0].equals(random_entry(TEMPLATE, derive_seed(11, "random_entry", 0)))
    assert all(
        a.equals(b) for a, b in zip(draws, random_entry_null(TEMPLATE, 200, seed=11), strict=True)
    )


def test_random_entry_places_a_single_episode_uniformly() -> None:
    template = pd.Series([0.0] * 7 + [1.0] * 4, dtype=float)  # 8 possible starts
    starts = Counter(holding_episodes(random_entry(template, seed))[0][0] for seed in range(4000))
    assert set(starts) == set(range(8))
    assert max(starts.values()) / min(starts.values()) < 1.3


def test_always_in_the_market_template_keeps_alternating_episodes() -> None:
    # MA-crossover style: long and short alternate without flat decisions; nothing can move,
    # so the null permutes the holding times within each side.
    template = pd.Series([1.0] * 3 + [-1.0] * 2 + [1.0] * 4 + [-1.0] * 1, dtype=float)
    for seed in range(20):
        draw = random_entry(template, seed)
        assert episode_profile(draw) == episode_profile(template)
        assert (draw != 0).all()


def test_random_entry_edge_cases() -> None:
    flat = pd.Series(np.zeros(5))
    assert (random_entry(flat, 1) == 0).all()
    with pytest.raises(ValueError, match="must not be missing"):
        random_entry(pd.Series([1.0, np.nan]), 1)


def test_screened_null_keeps_the_requested_trade_count() -> None:
    clock = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 11), date(2024, 3, 13))
    costs = CostModel.from_config(CFG, "xauusd")
    decisions = TEMPLATE.index
    quotes = pd.DataFrame(
        {
            "ts_utc": decisions + pd.Timedelta(seconds=2),
            "bid": 2000.0 + np.arange(len(decisions)),
            "ask": 2000.2 + np.arange(len(decisions)),
        }
    )
    template = run_vectorized(TEMPLATE, quotes, costs, clock, capital=100_000.0)
    assert len(template.trades) == 3
    for draw in random_entry_null(TEMPLATE, 25, seed=3):
        screened = run_vectorized(draw, quotes, costs, clock, capital=100_000.0)
        assert len(screened.trades) == len(template.trades)


def test_buy_and_hold_is_one_open_trade_paying_financing() -> None:
    clock = MarketClock.for_range(CFG.sessions_config(), date(2024, 3, 11), date(2024, 3, 15))
    costs = CostModel.from_config(CFG, "xauusd")
    daily = bars(2000, 2001, 2002)
    decisions = pd.date_range("2024-03-12 12:00", periods=3, freq="D", tz="UTC")
    exposure = rule_exposure(daily, RuleStrategyConfig(rule="buy_and_hold"))
    positions = positions_at(decisions, exposure.set_axis(decisions - pd.Timedelta(hours=1)))
    quotes = pd.DataFrame(
        {"ts_utc": decisions + pd.Timedelta(seconds=2), "bid": 2000.0, "ask": 2000.2}
    )
    result = run_vectorized(positions, quotes, costs, clock, capital=100_000.0)
    assert len(result.fills) == 1
    assert bool(result.trades["open"].iloc[0])
    assert len(result.financing) == 2  # two rollovers held
    assert (result.financing > 0).all()
