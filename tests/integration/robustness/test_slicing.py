"""ROB-006: pre-registered slicing — slices come from the registered, locked hypothesis version a
run tested (never from the caller), unknown slices are refused and regime slices wait for
REG-007; on planted edges the slices find where the P&L is, sessions follow DST, and an edge
earned in one year fails the R2 single-year gate."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine

from helpers.event_backtest import CAPITAL, CLOCK, exact_costs, random_quotes
from helpers.pipeline import REPO, config
from xq.backtest.vectorized import run_vectorized
from xq.core.config import AppConfig
from xq.robustness.slicing import (
    DeclaredSlices,
    SliceError,
    declared_slices,
    max_single_year_share,
    run_slices,
    session_buckets,
    slice_pnl,
)
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.hypotheses import register_hypothesis
from xq.tracking.registry import create_experiment, start_run

TEMPLATE = REPO / "experiments" / "hypotheses" / "TEMPLATE.yaml"


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


def register(cfg: AppConfig, engine: Engine, directory: Path, slices: list[str]) -> None:
    data: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    data.update({"id": "H-0001", "slices": slices})
    path = directory / "H-0001.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    register_hypothesis(cfg, engine, path)


def declared(cfg: AppConfig, engine: Engine, tmp_path: Path, slices: list[str]) -> DeclaredSlices:
    register(cfg, engine, tmp_path, slices)
    return declared_slices(engine, "H-0001")


def trading_days(start: str, end: str) -> pd.Index:
    return pd.Index(pd.bdate_range(start, end).date, name="trading_day")


def test_slices_come_from_the_locked_version_the_run_tested(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    register(cfg, engine, tmp_path, ["year", "Session"])
    experiment = create_experiment(engine, "H-0001", "first test")
    run = start_run(
        engine,
        experiment.experiment_id,
        kind="backtest",
        confirmatory=False,
        git_sha="abc",
        config_hash="h",
        config={},
        dataset_id=None,
        lock_hash="l",
        seed=1,
        host="test",
    )
    register(cfg, engine, tmp_path, ["volatility tercile"])  # edited after the run: version 2
    tested = run_slices(engine, run.run_id)
    assert (tested.version, tested.names) == (1, ("year", "session"))
    latest = declared_slices(engine, "H-0001")
    assert (latest.version, latest.names) == (2, ("volatility_tercile",))
    assert latest.yaml_hash != tested.yaml_hash
    with pytest.raises(TypeError, match="registered hypothesis"):
        DeclaredSlices("H-0001", 1, tested.yaml_hash, ("year",))  # a caller cannot pick slices


def test_unknown_slices_are_refused_and_regime_slices_wait_for_reg_007(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    regime = declared(cfg, engine, tmp_path, ["year", "trend/range regime"])
    assert regime.names == ("year", "trend_range_regime")  # a legitimate declaration ...
    pnl = pd.Series(1.0, index=trading_days("2024-01-02", "2024-01-31"))
    with pytest.raises(SliceError, match="REG-007"):  # ... refused when computed
        slice_pnl(
            regime,
            pnl,
            pd.DataFrame(),
            capital=CAPITAL,
            periods_per_year=252,
            sessions=cfg.sessions_config(),
        )
    register(cfg, engine, tmp_path, ["weekday"])
    with pytest.raises(SliceError, match="unknown slice"):
        declared_slices(engine, "H-0001")


def test_an_edge_earned_in_one_year_fails_the_single_year_gate(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    gates = cfg.gates_config()
    days = trading_days("2021-01-04", "2024-12-31")
    years = pd.to_datetime(pd.Index(days)).year
    noise = np.random.default_rng(1).normal(0.0, 50.0, len(days))
    lucky = pd.Series(np.where(years == 2022, 40.0, 0.0) + noise, index=days)
    steady = pd.Series(25.0 + noise, index=days)
    slices = declared(cfg, engine, tmp_path, ["year"])
    report = slice_pnl(
        slices,
        lucky,
        pd.DataFrame(),
        capital=CAPITAL,
        periods_per_year=252,
        sessions=cfg.sessions_config(),
    )
    table = report.tables["year"]
    assert list(table.index) == ["2021", "2022", "2023", "2024"]
    assert table.loc["2022", "pnl_share"] > 0.8
    assert table["pnl_share"].sum() == pytest.approx(1.0)
    assert not report.gate_check(gates).passed
    assert max_single_year_share(steady) < 0.35  # four similar years: about a quarter each
    assert (
        gates.criterion("R2", "max_single_year_pnl_share")
        .check(max_single_year_share(steady))
        .passed
    )
    assert np.isnan(max_single_year_share(-steady))  # no share of a loss; the gate fails


def test_volatility_terciles_find_an_edge_that_lives_in_high_volatility(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    days = trading_days("2021-01-04", "2023-12-29")
    rng = np.random.default_rng(2)
    sigma = pd.Series(rng.lognormal(np.log(0.01), 0.3, len(days)), index=days)
    high = sigma > sigma.quantile(2 / 3)
    pnl = pd.Series(np.where(high, 60.0, 0.0) + rng.normal(0.0, 40.0, len(days)), index=days)
    slices = declared(cfg, engine, tmp_path, ["volatility tercile"])
    table = slice_pnl(
        slices,
        pnl,
        pd.DataFrame(),
        capital=CAPITAL,
        periods_per_year=252,
        sessions=cfg.sessions_config(),
        sigma=sigma,
    ).tables["volatility_tercile"]
    assert list(table.index) == ["low", "mid", "high"]
    assert (table["days"] - len(days) / 3).abs().max() <= 1
    assert table.loc["high", "pnl_share"] > 0.8
    assert table.loc["high", "sharpe"] > 5 * max(table.loc["low", "sharpe"], 0.1)
    with pytest.raises(SliceError, match="sigma-hat"):
        slice_pnl(
            slices,
            pnl,
            pd.DataFrame(),
            capital=CAPITAL,
            periods_per_year=252,
            sessions=cfg.sessions_config(),
        )


def test_sessions_follow_dst_and_name_the_overlap(cfg: AppConfig) -> None:
    times = pd.DatetimeIndex(
        [
            "2024-01-16 03:00",  # Tokyo
            "2024-01-16 08:30",  # Tokyo still open, London open (GMT): no configured overlap
            "2024-01-16 12:30",  # London only: New York opens at 13:00 UTC in winter
            "2024-07-16 12:30",  # London and New York: it opens at 12:00 UTC in summer
            "2024-01-16 22:30",  # after New York's close, before Tokyo's open
        ],
        tz="UTC",
    )
    assert list(session_buckets(times, cfg.sessions_config())) == [
        "tokyo",
        "tokyo+london",
        "london",
        "london_new_york",
        "off_session",
    ]


def test_trades_of_a_screened_backtest_are_sliced_by_entry_session(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    quotes = random_quotes("2024-03-04", "2024-04-06", seed=5, every_s=60)
    days = pd.bdate_range("2024-03-04", "2024-03-28", tz="UTC")
    times = sorted(
        d + pd.Timedelta(hours=h, minutes=m)
        for d in days
        for h, m in ((3, 0), (4, 0), (14, 0), (15, 0))
    )
    positions = pd.Series([1.0, 0.0] * (len(times) // 2), index=pd.DatetimeIndex(times))
    result = run_vectorized(positions, quotes, exact_costs(), CLOCK, capital=CAPITAL)
    slices = declared(cfg, engine, tmp_path, ["session", "year"])
    report = slice_pnl(
        slices,
        result.daily["net_pnl"],
        result.trades,
        capital=CAPITAL,
        periods_per_year=252,
        sessions=cfg.sessions_config(),
    )
    sessions = report.tables["session"]
    assert set(sessions.index) == {"tokyo", "london_new_york"}  # 03:00 and 14:00 UTC entries
    assert sessions["trades"].sum() == len(days) * 2
    assert sessions["net_pnl"].sum() == pytest.approx(result.trades["pnl"].sum())
    assert report.tables["year"].loc["2024", "net_pnl"] == pytest.approx(
        result.daily["net_pnl"].sum()
    )
