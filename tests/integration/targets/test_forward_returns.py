"""TGT-002 end to end: a dataset with execution-aware forward-return targets on synthetic ticks.

Horizons are trading time (ADR 0026): label windows are checked on the market clock.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from helpers.ticks import dense_ticks, write_mt5
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.data.calendar import MarketClock
from xq.data.catalog import Catalog
from xq.datasets.builder import build_dataset, load_dataset
from xq.datasets.spec import load_spec
from xq.targets.base import market_horizon, target_values

FWD = {"name": "fwd_returns", "version": "v1"}
S = pd.Timedelta(seconds=1)
DELAY = pd.Timedelta(seconds=300)
CLOCK = MarketClock.for_range(config(REPO).sessions_config(), date(2024, 3, 10), date(2024, 3, 29))


def advance(times: pd.DatetimeIndex, duration: pd.Timedelta) -> pd.DatetimeIndex:
    """Instants `duration` of market time after `times`."""
    stamps = times.as_unit("ns").to_numpy("datetime64[ns]").view("int64")
    return pd.DatetimeIndex(pd.to_datetime(CLOCK.advance(stamps, duration.value), utc=True))


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    return config(tmp_path_factory.mktemp("fwd"))


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, clean_week_dir: Path) -> Iterator[Engine]:
    engine = validated_pipeline(cfg, clean_week_dir)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def targets(cfg: AppConfig, engine: Engine) -> pd.DataFrame:
    ref = build_dataset(cfg, engine, dataset_spec(target_set=FWD), git_sha="t")
    return load_dataset(cfg, ref.dataset_id, "targets")


def first_tick_at_or_after(ticks: pd.DataFrame, when: pd.Timestamp) -> pd.Series:
    return ticks[ticks["ts_utc"] >= when].iloc[0]


def test_values_match_a_hand_computation_from_ticks(cfg: AppConfig, targets: pd.DataFrame) -> None:
    t = pd.Timestamp("2024-03-13 14:00", tz="UTC")
    ticks = Catalog(cfg).load_ticks("mt5_primary", "xauusd", t, t + pd.Timedelta(hours=2))
    entry = first_tick_at_or_after(ticks, t + S)
    exit_ = first_tick_at_or_after(ticks, t + pd.Timedelta(hours=1) + S)
    long = target_values(targets, "fwd_ret_long_1h").loc[t]
    short = target_values(targets, "fwd_ret_short_1h").loc[t]
    mid = target_values(targets, "fwd_ret_mid_1h").loc[t]
    assert long["value"] == pytest.approx(np.log(exit_["bid"] / entry["ask"]))
    assert short["value"] == pytest.approx(np.log(entry["bid"] / exit_["ask"]))
    assert mid["value"] == pytest.approx(
        np.log((exit_["bid"] + exit_["ask"]) / (entry["bid"] + entry["ask"]))
    )
    assert long["label_start"] == entry["ts_utc"]
    assert long["label_end"] == exit_["ts_utc"]
    assert long["value"] < mid["value"]  # the spread is paid on both legs
    assert short["value"] < -mid["value"]

    normalized = target_values(targets, "fwd_ret_long_1h_vol").loc[t]
    assert normalized["value"] == pytest.approx(long["value"] / normalized["scale"])
    assert normalized["scale"] > 0


def test_label_windows_follow_trading_time(targets: pd.DataFrame) -> None:
    for horizon in ("15m", "1h", "4h", "1d"):
        one = target_values(targets, f"fwd_ret_mid_{horizon}")
        known = one["value"].notna()
        assert known.any()
        decision = pd.DatetimeIndex(one.index[known])
        market = market_horizon(horizon, pd.Timedelta(hours=23))  # 1d = 23 market hours
        entry, exit_ = advance(decision, S), advance(decision, market + S)
        starts = pd.DatetimeIndex(one.loc[known, "label_start"])
        ends = pd.DatetimeIndex(one.loc[known, "label_end"])
        assert ((starts >= entry) & (starts - entry <= DELAY)).all()
        assert ((ends >= exit_) & (ends - exit_ <= DELAY)).all()
        assert one.loc[known, "crosses_close"].any()  # some hold over the daily break
        assert not one.loc[~known, "crosses_close"].any()
    # Tuesday 16:00 EDT, 1 hour: the exit comes 1 second after Tuesday's 18:00 EDT reopen.
    held = target_values(targets, "fwd_ret_mid_1h").loc[pd.Timestamp("2024-03-12 20:00", tz="UTC")]
    assert np.isfinite(held["value"])
    assert held["crosses_close"]
    assert held["label_end"] >= pd.Timestamp("2024-03-12 22:00:01", tz="UTC")
    daily = target_values(targets, "fwd_ret_mid_1d")
    assert np.isfinite(daily.loc[pd.Timestamp("2024-03-14 14:00", tz="UTC"), "value"])
    # The data ends at the Friday close, so a Friday 1d label has no exit quote.
    assert np.isnan(daily.loc[pd.Timestamp("2024-03-15 14:00", tz="UTC"), "value"])


def test_friday_decisions_are_labelled_over_the_weekend(tmp_path: Path) -> None:
    ticks_dir = tmp_path / "ticks"
    ticks_dir.mkdir()
    ticks = dense_ticks("2024-03-12 22:00", "2024-03-19 21:00", seed=23, mean_interval_s=15)
    write_mt5(ticks, ticks_dir / "XAUUSD_weekend.csv")
    cfg = config(tmp_path)
    engine = validated_pipeline(cfg, ticks_dir)
    spec = dataset_spec(target_set=FWD, start="2024-03-14T00:00:00Z", end="2024-03-18T12:00:00Z")
    targets = load_dataset(cfg, build_dataset(cfg, engine, spec, git_sha="t").dataset_id, "targets")
    engine.dispose()

    friday = pd.Timestamp("2024-03-15 14:00", tz="UTC")
    daily = target_values(targets, "fwd_ret_long_1d").loc[friday]
    assert np.isfinite(daily["value"])
    assert daily["crosses_close"]
    # 1d = 23 market hours (ADR 0032): 7 on Friday, then 16 after the Sunday 18:00 EDT open,
    # ending Monday 14:00 UTC, the same session clock time one trading day later.
    assert daily["label_end"] >= pd.Timestamp("2024-03-18 14:00:01", tz="UTC")
    assert daily["label_end"] - pd.Timestamp("2024-03-18 14:00:01", tz="UTC") <= DELAY

    pre_close = target_values(targets, "fwd_ret_mid_15m").loc[
        pd.Timestamp("2024-03-15 20:45", tz="UTC")
    ]  # Friday 16:45 EDT: the 15 minutes end at the close, the exit is the Sunday reopen
    assert np.isfinite(pre_close["value"])
    assert pre_close["crosses_close"]
    assert pre_close["label_end"] >= pd.Timestamp("2024-03-17 22:00:01", tz="UTC")

    at_close = target_values(targets, "fwd_ret_mid_15m").loc[
        pd.Timestamp("2024-03-15 21:00", tz="UTC")
    ]  # decided at the Friday close, while the market is closed: no label (ADR 0032)
    assert np.isnan(at_close["value"])
    assert pd.isna(at_close["label_start"])
    assert not at_close["crosses_close"]


def test_manifest_reports_labels_with_late_fills(cfg: AppConfig, engine: Engine) -> None:
    ref = build_dataset(cfg, engine, dataset_spec(target_set=FWD), git_sha="t")
    targets = load_dataset(cfg, ref.dataset_id, "targets")
    report = ref.manifest["fill_delays"]
    assert report["threshold_s"] == cfg.datasets_config().fill_delay_report_s == 5
    assert set(report["targets"]) == set(targets["target"])
    for name, row in report["targets"].items():
        one = target_values(targets, name)
        labelled = one["value"].notna()
        assert row["labelled"] == labelled.sum() > 0
        assert row["delayed"] == (one.loc[labelled, "fill_delay_s"] > 5).sum()
        assert row["max_delay_s"] == pytest.approx(
            one.loc[labelled, "fill_delay_s"].max(), abs=1e-3
        )
        assert (one.loc[labelled, "fill_delay_s"] >= 0).all()
        assert one.loc[~labelled, "fill_delay_s"].isna().all()


def test_quotes_of_excluded_days_are_never_used(cfg: AppConfig, engine: Engine) -> None:
    exclusion = [{"trading_day": "2024-03-14", "reason": "test exclusion"}]
    ref = build_dataset(
        cfg, engine, dataset_spec(target_set=FWD, exclusions=exclusion), git_sha="t"
    )
    targets = load_dataset(cfg, ref.dataset_id, "targets")
    wednesday = pd.Timestamp("2024-03-13 14:00", tz="UTC")
    daily = target_values(targets, "fwd_ret_mid_1d")
    assert np.isnan(daily.loc[wednesday, "value"])  # its exit would be on the excluded day
    assert np.isfinite(target_values(targets, "fwd_ret_mid_1h").loc[wednesday, "value"])


def test_base_spec_names_the_forward_returns(tmp_path: Path) -> None:
    spec = load_spec(REPO / "experiments" / "configs" / "ds_base.yaml")
    assert spec.target_set is not None
    assert str(spec.target_set) == "fwd_returns.v1"


def test_cli_builds_a_dataset_with_targets(tmp_path: Path, clean_week_dir: Path) -> None:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--set",
        f"paths.root={tmp_path}",
        "--set",
        f"paths.migrations_dir={REPO / 'migrations'}",
        "--set",
        "logging.console=false",
    ]
    runner = CliRunner()
    for step in (
        ["ingest", "--path", str(clean_week_dir)],
        ["clean"],
        ["build-bars"],
        ["validate"],
    ):
        result = runner.invoke(app, [*common, step[0], "--source", "mt5_primary", *step[1:]])
        assert result.exit_code == 0, result.output
    spec_path = tmp_path / "ds.yaml"
    spec_path.write_text(yaml.safe_dump(dataset_spec(target_set=FWD).model_dump(mode="json")))
    built = runner.invoke(app, [*common, "dataset", "build", str(spec_path)])
    assert built.exit_code == 0, built.output
    shown = runner.invoke(app, [*common, "dataset", "show", built.stdout.split()[1]])
    manifest = yaml.safe_load(shown.stdout)
    assert "targets.parquet" in manifest["files"]
    assert len(manifest["targets"]) == 24
    assert "labels with a fill more than 5 s late:" in built.stdout
    assert "fwd_ret_long_1h: " in built.stdout


def test_a_decision_exactly_at_the_vault_start_gets_no_label(
    tmp_path: Path, clean_week_dir: Path
) -> None:
    vaulted = config(tmp_path, **{"vault.start": "2024-03-14T21:00:00Z"})
    engine = validated_pipeline(vaulted, clean_week_dir)
    spec = dataset_spec(
        target_set=FWD, start="2024-03-14T20:45:00Z", end="2024-03-14T21:00:00Z", warmup="1D"
    )
    ref = build_dataset(vaulted, engine, spec, git_sha="t")
    targets = load_dataset(vaulted, ref.dataset_id, "targets")
    assert targets.index.unique().tolist() == [pd.Timestamp("2024-03-14 21:00", tz="UTC")]
    assert targets["value"].isna().all()  # every fill would need quotes from the vault
    engine.dispose()
