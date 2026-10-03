"""MREG-004: a bundle's performance history. Daily rows are appended per source, in order, and
never rewritten or deleted (ADR 0060)."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from helpers.pipeline import REPO, config
from helpers.quality import repo_config
from xq.registry.bundles import (
    BundleStrategy,
    StrategyBundle,
    append_performance,
    performance_history,
    register_bundle,
)
from xq.registry.models import RegistryStateError
from xq.tracking.db import create_db_engine, upgrade_to_head

CFG = repo_config()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(config(tmp_path).database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


@pytest.fixture
def bundle_id(engine: Engine) -> str:
    content = StrategyBundle(
        instrument="xauusd",
        source="mt5_primary",
        base_timeframe="15m",
        price_basis="mid",
        feature_set_version="base.v1",
        strategy=BundleStrategy(
            kind="baseline_rule",
            name="buy_and_hold",
            config={"rule": {"rule": "buy_and_hold"}},
            signal_timeframe="1d",
        ),
        risk_config=CFG.risk_config().model_dump(mode="json"),
        cost_model_version="placeholder@0123456789ab",
    )
    ref = register_bundle(
        engine, content, name="hold", origin_run_id=None, origin_strategy=None, actor="test"
    )
    return ref.bundle_id


def days(start: str, n: int, returns: float = 0.001) -> pd.DataFrame:
    index = [d.date() for d in pd.bdate_range(start, periods=n)]
    return pd.DataFrame(
        {"net_return": np.full(n, returns), "net_pnl": np.full(n, returns * 1e5)}, index=index
    )


def test_daily_rows_are_appended_in_order_per_source(engine: Engine, bundle_id: str) -> None:
    assert (
        append_performance(engine, bundle_id, "backtest", days("2024-01-01", 5), run_id=None) == 5
    )
    assert append_performance(engine, bundle_id, "backtest", days("2024-01-08", 3), run_id="r") == 3
    assert append_performance(engine, bundle_id, "vault", days("2025-10-01", 2), run_id=None) == 2
    history = performance_history(engine, bundle_id)
    assert list(history["source"].unique()) == ["backtest", "vault"]
    backtest = performance_history(engine, bundle_id, "backtest")
    assert len(backtest) == 8
    assert backtest["trading_day"].iloc[0] == date(2024, 1, 1)
    assert backtest["run_id"].tolist() == [None] * 5 + ["r"] * 3
    assert backtest["trades"].isna().all()
    with pytest.raises(RegistryStateError, match="appended, never rewritten"):
        append_performance(engine, bundle_id, "backtest", days("2024-01-10", 2), run_id=None)
    with pytest.raises(RegistryStateError, match="unknown performance source"):
        append_performance(engine, bundle_id, "dream", days("2026-01-01", 1), run_id=None)
    bad = days("2024-02-01", 2)
    bad.iloc[1, 0] = np.nan
    with pytest.raises(RegistryStateError, match="finite net_return"):
        append_performance(engine, bundle_id, "backtest", bad, run_id=None)
    with pytest.raises(RegistryStateError, match="distinct and in order"):
        append_performance(
            engine, bundle_id, "paper", days("2026-01-01", 2).iloc[::-1], run_id=None
        )
    for statement in (
        "UPDATE bundle_performance SET net_return = 1",
        "DELETE FROM bundle_performance",
    ):
        with pytest.raises(IntegrityError, match="append-only"), engine.begin() as connection:
            connection.execute(text(statement))
