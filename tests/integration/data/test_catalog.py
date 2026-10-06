"""DATA-010: DuckDB catalog loaders return research data and enforce the vault."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from helpers.vault import seed_token
from xq.core.config import AppConfig, load_config
from xq.core.errors import ConfigError, NaiveTimestampError, VaultAccessError
from xq.core.types import Timeframe
from xq.data.bars import bar_set_dir, build_bar_sets, build_version
from xq.data.catalog import Catalog
from xq.data.clean import build_clean, clean_rules_version
from xq.data.raw_store import ingest
from xq.tracking.db import create_db_engine, upgrade_to_head

REPO = Path(__file__).resolve().parents[3]
VAULT = "2024-03-13 12:00"


def utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory, dense_week_dir: Path) -> AppConfig:
    root = tmp_path_factory.mktemp("catalog")
    cfg = load_config(
        "research",
        {
            "paths.root": str(root),
            "logging.file": None,
            "logging.console": False,
            "vault.start": f"{VAULT.replace(' ', 'T')}:00Z",
        },
        config_dir=REPO / "config",
    )
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    ingest(
        cfg,
        "mt5_primary",
        dense_week_dir,
        engine=engine,
        run_id="01RUNA0000000000000000000A",
        git_sha="test",
    )
    build_clean(cfg, engine, "mt5_primary")
    build_bar_sets(cfg, engine, "mt5_primary")
    engine.dispose()
    return cfg


@pytest.fixture(scope="module")
def catalog(cfg: AppConfig) -> Catalog:
    return Catalog(cfg)


def test_ticks_in_a_half_open_interval(cfg: AppConfig, catalog: Catalog) -> None:
    ticks = catalog.load_ticks(
        "mt5_primary", "xauusd", utc("2024-03-11 12:00"), utc("2024-03-11 13:00")
    )
    assert str(ticks["ts_utc"].dtype) == "datetime64[ns, UTC]"
    assert ticks["ts_utc"].is_monotonic_increasing
    assert ticks["ts_utc"].min() >= utc("2024-03-11 12:00")
    assert ticks["ts_utc"].max() < utc("2024-03-11 13:00")
    everything = pd.read_parquet(cfg.paths.resolve(cfg.paths.data_dir) / "clean")
    stamps = pd.to_datetime(everything["ts_utc"], unit="ns", utc=True)
    in_hour = (stamps >= utc("2024-03-11 12:00")) & (stamps < utc("2024-03-11 13:00"))
    assert len(ticks) == int(in_hour.sum()) > 0


@pytest.mark.parametrize("basis", ["bid", "ask", "mid"])
def test_bars_of_one_basis(cfg: AppConfig, catalog: Catalog, basis: str) -> None:
    bars = catalog.load_bars(
        "mt5_primary", "xauusd", "1h", basis, utc("2024-03-11 00:00"), utc("2024-03-12 00:00")
    )
    assert list(bars.columns[:6]) == [
        "bar_start_utc",
        "available_at_utc",
        "open",
        "high",
        "low",
        "close",
    ]
    assert len(bars) == 24 - 1  # 21:00-22:00 UTC is the daily break
    stored = pd.read_parquet(
        bar_set_dir(cfg, "mt5_primary", Timeframe.H1, _build(cfg))
    ).sort_values("bar_start_utc")
    stored = stored[
        stored["bar_start_utc"].between(utc("2024-03-11").value, utc("2024-03-12").value - 1)
    ]
    assert bars["close"].tolist() == stored[f"{basis}_close"].tolist()
    assert bars["tick_count"].tolist() == stored["tick_count"].tolist()
    assert isinstance(bars["trading_day"].iloc[0], date)


def test_requests_into_the_vault_are_refused(catalog: Catalog) -> None:
    with pytest.raises(VaultAccessError, match="vault starts"):
        catalog.load_ticks("mt5_primary", "xauusd", utc("2024-03-12"), utc("2024-03-14"))
    with pytest.raises(VaultAccessError, match="vault starts"):
        catalog.load_bars(
            "mt5_primary", "xauusd", "1m", "mid", utc("2024-03-13 11:00"), utc("2024-03-13 12:01")
        )
    with pytest.raises(VaultAccessError, match="gate token"):
        catalog.load_ticks("mt5_primary", "xauusd", utc("2024-03-13"), utc("2024-03-14"))


def test_a_gate_token_opens_the_vault_for_its_run(cfg: AppConfig) -> None:
    engine = create_db_engine(cfg.database_url())
    token = seed_token(engine, token_id="vt-catalog")
    opened = Catalog(cfg, engine=engine, run_id="01RUNV0000000000000000000V")
    ticks = opened.load_ticks(
        "mt5_primary", "xauusd", utc("2024-03-13"), utc("2024-03-14"), vault_token=token
    )
    assert ticks["ts_utc"].max() >= utc(VAULT)
    days = opened.load_bars(
        "mt5_primary",
        "xauusd",
        "1d",
        "mid",
        utc("2024-03-10"),
        utc("2024-03-15"),
        vault_token=token,
    )
    assert pd.Timestamp("2024-03-13").date() in days["trading_day"].tolist()
    # Without the engine and run the token cannot be verified; another run cannot reuse it.
    with pytest.raises(VaultAccessError, match="metadata database"):
        Catalog(cfg).load_ticks(
            "mt5_primary", "xauusd", utc("2024-03-13"), utc("2024-03-14"), vault_token=token
        )
    with pytest.raises(VaultAccessError, match="already used"):
        Catalog(cfg, engine=engine, run_id="01RUNW0000000000000000000W").load_ticks(
            "mt5_primary", "xauusd", utc("2024-03-13"), utc("2024-03-14"), vault_token=token
        )
    engine.dispose()


def test_nothing_from_the_vault_leaks_up_to_its_start(catalog: Catalog) -> None:
    end = utc(VAULT)
    ticks = catalog.load_ticks("mt5_primary", "xauusd", utc("2024-03-13 00:00"), end)
    assert ticks["ts_utc"].max() < end
    minutes = catalog.load_bars("mt5_primary", "xauusd", "1m", "mid", utc("2024-03-13 00:00"), end)
    assert (minutes["available_at_utc"] <= end).all()
    assert minutes["bar_start_utc"].max() == utc("2024-03-13 11:59")
    # The daily bar of 13 March straddles the vault start, so it contains vault ticks.
    days = catalog.load_bars("mt5_primary", "xauusd", "1d", "mid", utc("2024-03-10"), end)
    assert days["trading_day"].tolist() == [
        pd.Timestamp(d).date() for d in ("2024-03-11", "2024-03-12")
    ]


def test_bad_requests(catalog: Catalog) -> None:
    with pytest.raises(NaiveTimestampError):
        catalog.load_ticks("mt5_primary", "xauusd", pd.Timestamp("2024-03-11"), utc("2024-03-12"))
    with pytest.raises(ValueError, match="after start"):
        catalog.load_ticks("mt5_primary", "xauusd", utc("2024-03-12"), utc("2024-03-11"))
    with pytest.raises(ConfigError, match="carries 'xauusd'"):
        catalog.load_ticks("mt5_primary", "eurusd", utc("2024-03-11"), utc("2024-03-12"))


def test_empty_results_keep_their_schema(catalog: Catalog) -> None:
    ticks = catalog.load_ticks("mt5_primary", "xauusd", utc("2020-01-01"), utc("2020-01-02"))
    assert ticks.empty
    assert str(ticks["ts_utc"].dtype) == "datetime64[ns, UTC]"
    bars = catalog.load_bars(
        "mt5_primary",
        "xauusd",
        "5m",
        "bid",
        utc("2020-01-01"),
        utc("2020-01-02"),
        build="missing-build",
    )
    assert bars.empty
    assert list(bars.columns[:6]) == [
        "bar_start_utc",
        "available_at_utc",
        "open",
        "high",
        "low",
        "close",
    ]


def _build(cfg: AppConfig) -> str:
    return build_version(cfg.bars_config(), clean_rules_version(cfg))
