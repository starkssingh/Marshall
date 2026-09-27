"""VOL-002: realized variance, bipower variation and jumps match hand computations and recover
simulated diffusions with jumps; the diurnal factor recovers an injected intraday pattern and is
fitted on training rows only — perturbing test data leaves it unchanged."""

import math

import numpy as np
import pandas as pd
import pytest

from xq.core.config import DiurnalConfig
from xq.core.types import Timeframe
from xq.research.volatility.realized import (
    DiurnalFactor,
    intraday_returns,
    minutes_into_trading_day,
    realized_from_bars,
    realized_measures,
)

H1, D1 = Timeframe("1h"), Timeframe("1d")


def _bars(starts: pd.DatetimeIndex, log_close: np.ndarray, minutes: int = 1) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "bar_start_utc": starts,
            "available_at_utc": starts + pd.Timedelta(minutes=minutes),
            "close": 2000.0 * np.exp(log_close),
        }
    )


def test_measures_match_hand_computations() -> None:
    # six 1m bars on Tuesday 2024-03-05 from 14:00 UTC (one trading day, one UTC hour)
    starts = pd.date_range("2024-03-05 14:00", periods=6, freq="1min", tz="UTC")
    log_close = np.array([0.0, 0.001, -0.001, 0.002, 0.0015, 0.0005])
    frame = realized_from_bars(_bars(starts, log_close), D1)
    r = np.diff(log_close)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["n"] == 5
    assert row["rv"] == pytest.approx(np.sum(r**2))
    bv = math.pi / 2 * 5 / 4 * np.sum(np.abs(r[1:]) * np.abs(r[:-1]))
    assert row["bv"] == pytest.approx(bv)
    assert row["jump"] == pytest.approx(max(np.sum(r**2) - bv, 0.0))
    assert row["ret"] == pytest.approx(0.0005)
    # the trading day ends at 17:00 New York (22:00 UTC in March before the DST change)
    assert frame.index[0] == pd.Timestamp("2024-03-05 22:00", tz="UTC")
    assert row["period_start"] == pd.Timestamp("2024-03-04 22:00", tz="UTC")


def test_the_return_across_the_daily_roll_is_left_out() -> None:
    starts = pd.DatetimeIndex(
        pd.to_datetime(
            ["2024-03-05 21:58", "2024-03-05 21:59", "2024-03-05 23:00", "2024-03-05 23:01"],
            utc=True,
        )
    )
    returns = intraday_returns(_bars(starts, np.array([0.0, 0.001, 0.05, 0.051])))
    assert len(returns) == 2  # 21:59 and 23:01; the 50 bp break return is not intraday
    np.testing.assert_allclose(returns["ret"], [0.001, 0.001])
    daily = realized_measures(returns, D1)
    assert list(daily["n"]) == [1, 1]
    assert daily["bv"].isna().all()  # one return: no bipower pair


def test_hourly_rv_adds_up_to_daily_rv() -> None:
    starts = pd.date_range("2024-03-04 23:00", "2024-03-05 20:59", freq="1min", tz="UTC")
    rng = np.random.default_rng(1)
    bars = _bars(starts, np.cumsum(rng.standard_normal(len(starts)) * 1e-4))
    hourly, daily = realized_from_bars(bars, H1), realized_from_bars(bars, D1)
    assert len(daily) == 1
    assert len(hourly) == 22
    assert hourly["rv"].sum() == pytest.approx(daily["rv"].iloc[0])
    assert hourly["n"].sum() == daily["n"].iloc[0]
    assert (hourly.index >= hourly["period_start"] + pd.Timedelta(hours=1)).all()


def test_bipower_separates_jumps_from_the_diffusion() -> None:
    rng = np.random.default_rng(2)
    days, per_day, sigma = 60, 1380, 1e-4  # January to early March: no DST change
    rv, bv, jump = [], [], []
    for d in range(days):
        day = pd.Timestamp("2024-01-02") + pd.Timedelta(days=d)  # 18:00 New York, DST-proof
        start = day.replace(hour=18).tz_localize("America/New_York").tz_convert("UTC")
        starts = pd.date_range(start, periods=per_day, freq="1min")
        r = rng.standard_normal(per_day) * sigma
        r[per_day // 2] += 0.005  # one jump of 50 bp a day
        frame = realized_from_bars(_bars(starts, np.cumsum(r)), D1)
        assert len(frame) == 1  # 23 market hours inside one trading day
        row = frame.iloc[0]
        rv.append(row["rv"])
        bv.append(row["bv"])
        jump.append(row["jump"])
    diffusive = (per_day - 1) * sigma**2
    # BV is jump-robust only as the sampling gets finer: the two pairs touching the jump add
    # (pi / 2) n / (n - 1) * 2 J E|r|, with E|r| = sigma sqrt(2 / pi); RV adds all of J^2
    n = per_day - 1
    contamination = math.pi / 2 * n / (n - 1) * 2 * 0.005 * sigma * math.sqrt(2 / math.pi)
    assert np.mean(bv) == pytest.approx(diffusive + contamination, rel=0.02)
    assert np.mean(rv) == pytest.approx(diffusive + 0.005**2, rel=0.02)
    assert np.mean(jump) == pytest.approx(0.005**2 - contamination, rel=0.05)
    assert contamination < 0.06 * 0.005**2  # BV moves by a small fraction of the jump


def _diurnal_sample(days: int, seed: int) -> tuple[pd.DataFrame, dict[int, float]]:
    """5m returns on 23 market hours a day with an injected hourly variance pattern."""
    rng = np.random.default_rng(seed)
    pattern = {h: 1.0 + 1.5 * math.exp(-((h - 15) ** 2) / 8) for h in range(1, 24)}
    rows = []
    for d in range(days):
        day = pd.Timestamp("2024-01-08") + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        opened = (day - pd.Timedelta(days=1)).replace(hour=18).tz_localize("America/New_York")
        starts = pd.date_range(opened, periods=23 * 12, freq="5min").tz_convert("UTC")
        hours = minutes_into_trading_day(starts) // 60
        scale = np.sqrt([pattern[int(h)] for h in hours])
        rows.append(pd.DataFrame({"start": starts, "r": rng.standard_normal(len(starts)) * scale}))
    frame = pd.concat(rows, ignore_index=True)
    frame["available_at"] = frame["start"] + pd.Timedelta(minutes=5)
    return frame, pattern


CFG = DiurnalConfig(day_standardized=True, min_count=20)


def test_the_diurnal_factor_recovers_an_injected_pattern() -> None:
    frame, pattern = _diurnal_sample(90, seed=3)
    factor = DiurnalFactor.fit(
        frame["start"],
        frame["available_at"],
        frame["r"] ** 2,
        CFG,
        bucket_minutes=60,
        train_end=frame["available_at"].iloc[-1],
    )
    truth = np.array([pattern[h] for h in range(1, 24)])
    truth /= truth.mean()  # every hour has the same number of rows
    fitted = np.array([factor.factors[h] for h in range(1, 24)])
    np.testing.assert_allclose(fitted, truth, rtol=0.12)
    assert factor.next_buckets(23, 2) == [1, 2]  # 16:00 New York is followed by the 18:00 open
    assert factor.variance_factor(pd.DatetimeIndex(["2024-02-01 03:00"], tz="UTC"))[0] == (
        pytest.approx(
            factor.factors[
                int(
                    minutes_into_trading_day(pd.DatetimeIndex(["2024-02-01 03:00"], tz="UTC"))[0]
                    // 60
                )
            ]
        )
    )


def test_the_diurnal_factor_is_fitted_on_training_rows_only() -> None:
    frame, _ = _diurnal_sample(90, seed=4)
    train_end = frame["available_at"].iloc[len(frame) // 2]
    base = DiurnalFactor.fit(
        frame["start"],
        frame["available_at"],
        frame["r"] ** 2,
        CFG,
        bucket_minutes=60,
        train_end=train_end,
    )
    perturbed = frame["r"].to_numpy().copy()
    test_rows = (frame["available_at"] > train_end).to_numpy()
    perturbed[test_rows] *= np.linspace(1, 20, test_rows.sum())  # change the test data's pattern
    again = DiurnalFactor.fit(
        frame["start"],
        frame["available_at"],
        perturbed**2,
        CFG,
        bucket_minutes=60,
        train_end=train_end,
    )
    assert again.factors == base.factors
    assert again.n_train == base.n_train == int((~test_rows).sum())
    full_sample = DiurnalFactor.fit(  # the leak this guards against would move the factor
        frame["start"],
        frame["available_at"],
        perturbed**2,
        CFG,
        bucket_minutes=60,
        train_end=frame["available_at"].iloc[-1],
    )
    assert full_sample.factors != base.factors


def test_sparse_buckets_keep_a_factor_of_one_and_bad_inputs_are_refused() -> None:
    frame, _ = _diurnal_sample(3, seed=5)  # 36 rows per hour: below a min_count of 50
    sparse = DiurnalFactor.fit(
        frame["start"],
        frame["available_at"],
        frame["r"] ** 2,
        DiurnalConfig(min_count=50),
        bucket_minutes=60,
        train_end=frame["available_at"].iloc[-1],
    )
    assert set(sparse.factors.values()) == {1.0}
    with pytest.raises(ValueError, match="no training row"):
        DiurnalFactor.fit(
            frame["start"],
            frame["available_at"],
            frame["r"] ** 2,
            CFG,
            bucket_minutes=60,
            train_end=pd.Timestamp("2000-01-01", tz="UTC"),
        )
    with pytest.raises(ValueError, match="non-negative"):
        DiurnalFactor.fit(
            frame["start"],
            frame["available_at"],
            frame["r"],
            CFG,
            bucket_minutes=60,
            train_end=frame["available_at"].iloc[-1],
        )
