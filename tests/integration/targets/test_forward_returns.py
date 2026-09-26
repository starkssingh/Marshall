"""TGT-002 end to end: a dataset with execution-aware forward-return targets on synthetic ticks."""

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.data.catalog import Catalog
from xq.datasets.builder import build_dataset, load_dataset
from xq.datasets.spec import load_spec
from xq.targets.base import target_values

FWD = {"name": "fwd_returns", "version": "v1"}
S = pd.Timedelta(seconds=1)


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


def test_label_windows_are_bounded_and_markets_closures_give_no_label(
    targets: pd.DataFrame,
) -> None:
    for horizon in ("15m", "1h", "4h", "1d"):
        one = target_values(targets, f"fwd_ret_mid_{horizon}")
        known = one["value"].notna()
        assert known.any()
        decision = one.index[known]
        assert (one.loc[known, "label_start"] >= decision + S).all()
        limit = pd.Timedelta(horizon) + S + pd.Timedelta(seconds=300)
        assert (one.loc[known, "label_end"] - decision <= limit).all()
    daily = target_values(targets, "fwd_ret_mid_1d")
    thursday = pd.Timestamp("2024-03-14 14:00", tz="UTC")
    friday = pd.Timestamp("2024-03-15 14:00", tz="UTC")
    assert np.isfinite(daily.loc[thursday, "value"])
    assert np.isnan(daily.loc[friday, "value"])  # the exit falls on the weekend
    assert np.isnan(
        target_values(targets, "fwd_ret_mid_1h").loc[
            pd.Timestamp("2024-03-12 20:00", tz="UTC"), "value"
        ]
    )  # the exit falls in the daily break


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
