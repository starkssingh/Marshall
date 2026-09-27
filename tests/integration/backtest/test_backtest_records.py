"""BT-010: a backtest of each tier is written as a report, its ledger stored and the backtest
recorded in the ``backtests`` table of the run that produced it."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

from helpers.event_backtest import CLOCK, exact_costs, random_quotes
from helpers.pipeline import REPO, config
from xq.backtest.engine import MarketData, run_event_backtest
from xq.backtest.report import cost_model_version, write_backtest
from xq.backtest.strategies import RuleStrategy, signal_frame
from xq.backtest.vectorized import run_vectorized
from xq.core.config import AppConfig
from xq.core.types import Timeframe
from xq.models.baselines import RuleStrategyConfig, rule_exposure
from xq.risk.engine import RiskEngine
from xq.tracking import registry
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.runs import experiment_run

RULE = RuleStrategyConfig(rule="time_series_momentum", params={"lookback": 8})


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(
        engine, "H-0001", title="t", family_id="baselines", yaml_text="x\n"
    )
    yield engine
    engine.dispose()


def test_both_tiers_are_recorded_with_report_and_ledger(cfg: AppConfig, engine: Engine) -> None:
    q = random_quotes("2024-03-11 00:00", "2024-03-16 00:00", seed=2, every_s=30)
    data = MarketData.from_ticks(q, Timeframe.M15)
    capital = cfg.backtest_config().capital_usd
    costs = exact_costs()
    screener = run_vectorized(
        rule_exposure(signal_frame(data.bars), RULE), q, costs, CLOCK, capital=capital
    )
    event = run_event_backtest(
        RuleStrategy(RULE),
        data,
        costs,
        CLOCK,
        capital=capital,
        margin_rate=cfg.backtest_config().event_config().margin_rate,
        risk=RiskEngine.from_config(cfg),
    )
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="backtest", seed=1, exploratory=True
    ) as run:
        common = {"strategy_id": "time_series_momentum", "strategy_version": "1"}
        vector = write_backtest(run, screener, costs, title="screener", name="screener", **common)
        events = write_backtest(run, event, costs, title="event", name="event", **common)
    records = registry.list_backtests(engine, run.run_id)
    assert [r.tier for r in records] == ["vectorized", "event"]
    assert [r.backtest_id for r in records] == [vector.backtest_id, events.backtest_id]
    assert records[0].ledger_path is None
    ledger = pd.read_parquet(str(records[1].ledger_path))
    assert (ledger["kind"] == "order").sum() > 0
    assert records[1].cost_model_version == cost_model_version(costs)
    assert records[1].cost_model_version.startswith("test@")
    assert records[1].start == pd.Timestamp("2024-03-11", tz="UTC")
    assert records[1].end == pd.Timestamp("2024-03-15", tz="UTC")
    assert records[1].metrics["net_profit"] == pytest.approx(
        event.daily["equity"].iloc[-1] - capital
    )
    assert "ambiguous_share" in records[1].metrics
    assert Path(records[1].report_path, "summary.md").is_file()
    kinds = {a.kind for a in registry.list_artifacts(engine, run.run_id)}
    assert kinds == {"backtest_report", "backtest_ledger"}
