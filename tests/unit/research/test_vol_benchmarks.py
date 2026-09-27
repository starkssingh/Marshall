"""VOL-003: the fixed benchmarks — EWMA and rolling RV by hand, HAR coefficients recovered on a
simulated HAR process and fitted on training rows whose future is also training, every benchmark
causal, and the diurnal adjustment fitted on training periods only."""

import math

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO
from helpers.simulate import garch_periods
from xq.core.config import DiurnalConfig, load_config
from xq.core.errors import NaiveTimestampError
from xq.core.types import Timeframe
from xq.models.volatility import VolForecaster
from xq.research.volatility.benchmarks import (
    Deseasonalized,
    Ewma,
    Har,
    RollingRV,
    benchmark_forecasters,
)

INDEX = pd.date_range("2024-01-02 22:00", periods=6, freq="D", tz="UTC", name="decision_time")
SMALL = pd.DataFrame(
    {
        "period_start": INDEX - pd.Timedelta(days=1),
        "ret": [0.01, -0.02, 0.0, 0.03, -0.01, 0.02],
        "rv": [1e-4, 4e-4, 2e-4, 9e-4, 1e-4, 3e-4],
    },
    index=INDEX,
)


def test_ewma_by_hand() -> None:
    model = Ewma(0.9).fit(SMALL.iloc[:3])
    initial = np.mean(np.square([0.01, -0.02, 0.0]))
    assert model.initial == pytest.approx(initial)
    forecast = model.predict_variance(SMALL, 2)
    state = initial
    for t, r in enumerate(SMALL["ret"]):
        state = 0.9 * state + 0.1 * r**2
        assert forecast.iloc[t] == pytest.approx(2 * state)
    assert model.predict(SMALL, 2).iloc[-1] == pytest.approx(math.sqrt(forecast.iloc[-1]))
    assert model.name == "ewma_0.9"


def test_rolling_rv_by_hand() -> None:
    forecast = RollingRV(3).fit(SMALL).predict_variance(SMALL, 1)
    assert forecast.iloc[:2].isna().all()
    assert forecast.iloc[2] == pytest.approx(np.mean([1e-4, 4e-4, 2e-4]))
    assert forecast.iloc[5] == pytest.approx(np.mean([9e-4, 1e-4, 3e-4]))


def _har_process(n: int, beta: tuple[float, float, float, float], seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rv = np.full(n, 1.0)
    for t in range(22, n - 1):
        mean = beta[0] + beta[1] * rv[t] + beta[2] * rv[t - 4 : t + 1].mean()
        mean += beta[3] * rv[t - 21 : t + 1].mean()
        rv[t + 1] = mean * rng.gamma(10.0, 0.1)  # multiplicative noise with mean 1
    index = pd.date_range("2000-01-03 22:00", periods=n, freq="D", tz="UTC", name="decision_time")
    return pd.DataFrame(
        {"period_start": index - pd.Timedelta(days=1), "ret": np.sqrt(rv) * 0.0, "rv": rv},
        index=index,
    )


def test_har_recovers_its_coefficients() -> None:
    truth = (0.1, 0.4, 0.3, 0.2)
    periods = _har_process(20_000, truth, seed=1)
    har = Har([1, 5, 22], floor_share=0.01).fit(periods)
    np.testing.assert_allclose(har.coefficients(1), truth, atol=0.04)


def test_har_is_fitted_on_training_rows_whose_future_is_training() -> None:
    periods = _har_process(600, (0.1, 0.4, 0.3, 0.2), seed=2)
    har = Har([1, 5, 22], floor_share=0.01).fit(periods.iloc[:400])
    rv = periods["rv"].to_numpy()[:400]
    x = np.column_stack(
        [np.ones(400)]
        + [pd.Series(rv).rolling(c, min_periods=c).mean().to_numpy() for c in (1, 5, 22)]
    )
    y = np.full(400, np.nan)
    y[:397] = rv[1:398] + rv[2:399] + rv[3:400]  # three periods ahead, inside the training rows
    usable = np.isfinite(y) & np.isfinite(x).all(axis=1)
    expected, *_ = np.linalg.lstsq(x[usable], y[usable], rcond=None)
    np.testing.assert_allclose(har.coefficients(3), expected)


def test_har_forecasts_are_floored() -> None:
    periods = _har_process(400, (0.1, 0.4, 0.3, 0.2), seed=3)
    har = Har([1, 5, 22], floor_share=0.5).fit(periods)
    shocked = periods.copy()
    shocked["rv"] = 0.0
    floor = 0.5 * periods["rv"].mean() * 2
    assert (har.predict_variance(shocked, 2).dropna() >= floor - 1e-12).all()


@pytest.mark.parametrize(
    "model",
    [RollingRV(22), Ewma(0.94), Har([1, 5, 22], 0.01)],
    ids=lambda m: m.name,
)
def test_benchmarks_are_causal(model: VolForecaster) -> None:
    periods, _ = garch_periods(600, omega=0.05, alpha=0.08, beta=0.9, seed=4)
    model.fit(periods.iloc[:300])
    base = model.predict_variance(periods, 5)
    changed = periods.copy()
    changed.iloc[450:, changed.columns.get_indexer(["ret", "rv"])] *= 7.0
    after = model.predict_variance(changed, 5)
    pd.testing.assert_series_equal(base.iloc[:450], after.iloc[:450])
    assert not np.allclose(base.iloc[451:], after.iloc[451:])


def _hourly(days: int, seed: int) -> pd.DataFrame:
    """Hourly periods on 23 market hours a day with a strong diurnal pattern (factor 1 to 4)."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        day = pd.Timestamp("2024-01-08") + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        opened = (day - pd.Timedelta(days=1)).replace(hour=18).tz_localize("America/New_York")
        starts = pd.date_range(opened, periods=23, freq="1h").tz_convert("UTC")
        pattern = 1.0 + 3.0 * (np.arange(23) >= 14)  # the New York morning is four times busier
        rv = pattern * 1e-6 * rng.gamma(5.0, 0.2, 23)
        ret = np.sqrt(rv) * rng.standard_normal(23)
        rows.append(pd.DataFrame({"period_start": starts, "ret": ret, "rv": rv}))
    frame = pd.concat(rows, ignore_index=True)
    frame.index = pd.DatetimeIndex(
        frame["period_start"] + pd.Timedelta(hours=1), name="decision_time"
    )
    return frame


def test_the_diurnal_adjustment_is_fitted_on_training_periods_only() -> None:
    periods = _hourly(60, seed=5)
    split = len(periods) // 2
    model = Deseasonalized(Ewma(0.94), DiurnalConfig(min_count=10))
    model.fit(periods.iloc[:split])
    assert model.factor is not None
    factors = dict(model.factor.factors)
    busy = np.mean([factors[b] for b in range(15, 24)])  # 18:00 New York is bucket 1
    quiet = np.mean([factors[b] for b in range(1, 15)])
    assert busy / quiet == pytest.approx(4.0, rel=0.15)
    base = model.predict_variance(periods, 1)
    changed = periods.copy()
    changed.iloc[split:, changed.columns.get_indexer(["ret", "rv"])] *= 9.0
    again = Deseasonalized(Ewma(0.94), DiurnalConfig(min_count=10)).fit(changed.iloc[:split])
    assert again.factor is not None
    assert again.factor.factors == factors  # test data cannot move the factor
    pd.testing.assert_series_equal(
        base.iloc[:split], again.predict_variance(changed, 1).iloc[:split]
    )


def test_the_diurnal_adjustment_scales_forecasts_by_the_next_hours() -> None:
    periods = _hourly(60, seed=6)
    model = Deseasonalized(Ewma(0.94), DiurnalConfig(min_count=10)).fit(periods)
    forecast = model.predict_variance(periods, 1)
    buckets = pd.Series(model.factor.buckets(periods["period_start"]), index=periods.index)
    # the hour before the busy morning forecasts a busier next hour than the busy hours' last one
    before_busy = forecast[buckets == 14].mean()
    last_busy = forecast[buckets == 23].mean()
    assert before_busy > 2 * last_busy


def test_the_configured_benchmarks() -> None:
    cfg = load_config("dev", config_dir=REPO / "config").volatility_config()
    daily = benchmark_forecasters(cfg, Timeframe("1d"))
    assert list(daily) == ["rolling_22", "ewma_0.94", "ewma_0.97", "har"]
    assert list(daily) == cfg.benchmark_names()
    har = daily["har"]()
    assert isinstance(har, Har)
    assert har.components == [1, 5, 22]
    hourly = benchmark_forecasters(cfg, Timeframe("1h"))
    made = hourly["har"]()
    assert isinstance(made, Deseasonalized)
    assert isinstance(made.inner, Har)
    assert made.inner.components == [1, 23, 115]
    assert made.name == "har"
    assert cfg.selection.default == "ewma_0.94"


def test_the_interface_refuses_bad_periods() -> None:
    naive = SMALL.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(NaiveTimestampError):
        Ewma(0.94).fit(naive)
    with pytest.raises(ValueError, match="increasing"):
        Ewma(0.94).fit(SMALL.iloc[::-1])
    with pytest.raises(ValueError, match="lack columns"):
        Ewma(0.94).fit(SMALL.drop(columns=["rv"]))
    with pytest.raises(ValueError, match="at least 1"):
        Ewma(0.94).fit(SMALL).predict_variance(SMALL, 0)
    with pytest.raises(RuntimeError, match="fit"):
        Har([1], 0.01).predict_variance(SMALL, 1)
