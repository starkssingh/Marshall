"""DS-005: dataset builder, manifests and versioning."""

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.datasets import QUALITY_RUN, dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config, run_pipeline
from xq.cli.main import app
from xq.core.config import AppConfig, load_config
from xq.core.errors import ConfigError, VaultAccessError
from xq.core.time import trading_day, trading_day_bounds
from xq.core.types import Timeframe
from xq.data.catalog import Catalog
from xq.datasets.builder import (
    DatasetIntegrityError,
    NoDatasetDataError,
    build_dataset,
    config_digest,
    load_dataset,
    read_manifest,
    verify_dataset,
)
from xq.datasets.spec import dataset_id, load_spec
from xq.features.registry import warmup_bars
from xq.tracking.db import session_factory
from xq.tracking.models import DatasetVersion


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    return config(tmp_path_factory.mktemp("builder"))


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, clean_week_dir: Path) -> Iterator[Engine]:
    engine = validated_pipeline(cfg, clean_week_dir)
    yield engine
    engine.dispose()


def test_build_materializes_features_manifest_and_record(cfg: AppConfig, engine: Engine) -> None:
    ref = build_dataset(cfg, engine, dataset_spec(), git_sha="abc123")
    assert not ref.reproduced
    assert {p.name for p in ref.path.iterdir()} == {
        "features.parquet",
        "spec.yaml",
        "manifest.json",
    }
    assert ref.spec.is_resolved
    assert ref.spec.quality_run_id == QUALITY_RUN
    assert ref.spec.config_digest == config_digest(cfg, dataset_spec())

    catalog = Catalog(cfg)
    bars = catalog.load_bars(
        "mt5_primary",
        "xauusd",
        "15m",
        "mid",
        pd.Timestamp("2024-03-12", tz="UTC"),
        pd.Timestamp("2024-03-15 20:00", tz="UTC"),
    )
    bars = bars[bars["is_complete"]]
    features = load_dataset(cfg, ref.dataset_id)
    assert features.index.name == "decision_time"
    assert str(features.index.tz) == "UTC"
    assert features.index.tolist() == bars["available_at_utc"].tolist()
    assert features["close"].tolist() == bars["close"].tolist()
    calendar = {"trading_day", "in_london_new_york", "minutes_to_lbma_pm", "in_rollover_window"}
    assert calendar <= set(features.columns)
    assert features["in_new_york"].dtype == bool
    for tf in ("1h", "4h"):
        provenance = features[f"ctx_{tf}_available_at"]
        assert (provenance.dropna() <= features.index[provenance.notna()]).all()
        assert provenance.notna().all()  # the warm-up makes a context bar available from the start

    manifest = ref.manifest
    assert manifest["dataset_id"] == ref.dataset_id
    assert manifest["row_count"] == len(features)
    assert manifest["decision_time_first"] == str(features.index[0])
    assert manifest["quality_run_ids"] == [QUALITY_RUN]
    assert manifest["bar_set_ids"] == [
        f"mt5_primary:xauusd:{tf}:{ref.spec.bar_build}" for tf in ("15m", "1h", "4h")
    ]
    assert manifest["excluded_partitions"] == []
    assert manifest["warn_partitions"] == []
    assert manifest["included_partitions"] == 5  # warm-up day, window days and context days
    assert manifest["git_sha"] == "abc123"
    assert manifest["code_versions"] == {"dataset": 1, "features:base.v1": 1}
    assert manifest["columns"]["features"]["close"] == "float64"

    with session_factory(engine)() as session:
        record = session.get(DatasetVersion, ref.dataset_id)
    assert record is not None
    assert record.sha256 == manifest["sha256"]
    assert record.row_count == len(features)
    assert record.feature_set_version == "base.v1"
    assert record.quality_run_ids == [QUALITY_RUN]


def test_rebuild_reproduces_the_sha256(cfg: AppConfig, engine: Engine) -> None:
    first = build_dataset(cfg, engine, dataset_spec(), git_sha="abc123")
    second = build_dataset(cfg, engine, dataset_spec(), git_sha="def456")
    assert second.reproduced
    assert second.dataset_id == first.dataset_id
    assert second.manifest["sha256"] == first.manifest["sha256"]
    assert second.manifest["git_sha"] == "abc123"  # the stored dataset is kept as it was


def test_stored_spec_rebuilds_the_same_dataset(cfg: AppConfig, engine: Engine) -> None:
    ref = build_dataset(cfg, engine, dataset_spec(), git_sha="abc123")
    stored = load_spec(ref.path / "spec.yaml")
    assert stored == ref.spec
    again = build_dataset(cfg, engine, stored, git_sha="abc123")
    assert again.dataset_id == ref.dataset_id
    assert again.reproduced


def test_tampering_is_detected(cfg: AppConfig, engine: Engine) -> None:
    ref = build_dataset(cfg, engine, dataset_spec(name="ds_tamper"), git_sha="abc123")
    features = ref.path / "features.parquet"
    original = features.read_bytes()
    features.write_bytes(original + b"\0")
    with pytest.raises(DatasetIntegrityError, match="differs"):
        load_dataset(cfg, ref.dataset_id)
    with pytest.raises(DatasetIntegrityError, match="differs"):
        verify_dataset(cfg, ref.dataset_id)
    with pytest.raises(DatasetIntegrityError, match="differs"):
        build_dataset(cfg, engine, dataset_spec(name="ds_tamper"), git_sha="abc123")
    features.write_bytes(original)
    assert verify_dataset(cfg, ref.dataset_id)["dataset_id"] == ref.dataset_id


def test_excluded_days_are_dropped_and_recorded(cfg: AppConfig, engine: Engine) -> None:
    excluded = {"trading_day": "2024-03-13", "reason": "test exclusion"}
    ref = build_dataset(cfg, engine, dataset_spec(exclusions=[excluded]), git_sha="abc123")
    features = load_dataset(cfg, ref.dataset_id)
    full = load_dataset(cfg, build_dataset(cfg, engine, dataset_spec(), git_sha="x").dataset_id)
    day = pd.Timestamp("2024-03-13 21:00", tz="UTC")  # end of trading day 2024-03-13
    start = pd.Timestamp("2024-03-12 21:00", tz="UTC")
    assert not ((features.index > start) & (features.index <= day)).any()
    assert len(full) - len(features) == ((full.index > start) & (full.index <= day)).sum()
    assert ref.manifest["excluded_partitions"] == [{**excluded, "failing_checks": []}]
    assert ref.spec.excluded_days == {date(2024, 3, 13)}


def test_window_into_the_vault_is_refused(tmp_path: Path, clean_week_dir: Path) -> None:
    cfg = config(tmp_path, **{"vault.start": "2024-03-14T21:00:00Z"})
    engine = validated_pipeline(cfg, clean_week_dir)
    with pytest.raises(VaultAccessError, match="vault starts"):
        build_dataset(cfg, engine, dataset_spec(), git_sha="x")
    ref = build_dataset(cfg, engine, dataset_spec(end="2024-03-14T21:00:00Z"), git_sha="x")
    assert load_dataset(cfg, ref.dataset_id).index.max() <= pd.Timestamp(cfg.vault.start)
    engine.dispose()


def test_a_quality_run_is_required(tmp_path: Path, clean_week_dir: Path) -> None:
    cfg = config(tmp_path)
    engine = run_pipeline(cfg, clean_week_dir, spreads=False)
    with pytest.raises(NoDatasetDataError, match="xq validate"):
        build_dataset(cfg, engine, dataset_spec(), git_sha="x")
    engine.dispose()


def test_invalid_requests(cfg: AppConfig, engine: Engine) -> None:
    with pytest.raises(ValueError, match="bar_build"):
        build_dataset(cfg, engine, dataset_spec(bar_build="b1-ffffffff"), git_sha="x")
    with pytest.raises(ConfigError, match="unknown feature set"):
        build_dataset(
            cfg, engine, dataset_spec(feature_set={"name": "base", "version": "v9"}), git_sha="x"
        )
    with pytest.raises(ConfigError, match="carries 'xauusd'"):
        build_dataset(cfg, engine, dataset_spec(instrument="eurusd"), git_sha="x")
    with pytest.raises(NoDatasetDataError, match="no complete 15m bars"):
        build_dataset(
            cfg,
            engine,
            # The daily break (17:00-18:00 New York): no bars start inside it.
            dataset_spec(start="2024-03-12T21:00:00Z", end="2024-03-12T22:00:00Z", warmup="0s"),
            git_sha="x",
        )
    with pytest.raises(NoDatasetDataError, match="not found"):
        read_manifest(cfg, "ds-0000000000000000")


def test_configuration_changes_the_id(cfg: AppConfig, engine: Engine, tmp_path: Path) -> None:
    other = config(tmp_path, **{"sessions.market.open": "18:05"})
    assert config_digest(other, dataset_spec()) != config_digest(cfg, dataset_spec())
    ref = build_dataset(cfg, engine, dataset_spec(), git_sha="x")
    moved = ref.spec.model_copy(update={"config_digest": config_digest(other, dataset_spec())})
    assert dataset_id(moved) != dataset_id(ref.spec)


def test_cli_dataset_build_and_show(tmp_path: Path, clean_week_dir: Path) -> None:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--profile",
        "research",
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
    spec_path.write_text(yaml.safe_dump(dataset_spec().model_dump(mode="json")))
    built = runner.invoke(app, [*common, "dataset", "build", str(spec_path)])
    assert built.exit_code == 0, built.output
    assert " built: " in built.stdout
    ds_id = built.stdout.split()[1]
    again = runner.invoke(app, [*common, "dataset", "build", str(spec_path)])
    assert "reproduced (identical content)" in again.stdout
    shown = runner.invoke(app, [*common, "dataset", "show", ds_id])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["dataset_id"] == ds_id
    missing = runner.invoke(app, [*common, "dataset", "show", "ds-0000000000000000"])
    assert missing.exit_code == 2
    assert Timeframe("15m") is Timeframe.M15


def test_base_spec_is_valid() -> None:
    spec = load_spec(REPO / "experiments" / "configs" / "ds_base.yaml")
    assert spec.name == "ds_base"
    assert spec.source == "dukascopy"  # the primary research feed (ADR 0057)
    assert spec.end == pd.Timestamp("2025-09-25T21:00:00Z")
    # the owner's window, fixed before any result (ADR 0062): the first trading day of 2015
    start = pd.Timestamp(spec.start)
    assert start == pd.Timestamp("2015-01-01T22:00:00Z")
    assert trading_day(start) == date(2015, 1, 2)
    assert trading_day_bounds(date(2015, 1, 2))[0] == start
    assert [tf.value for tf in spec.context_timeframes] == ["1h", "4h", "1d"]


def test_core_spec_is_ds_base_with_the_feature_set_core_v2() -> None:
    # C-33 (ADR 0067): a spec only, not built until DQ-008; ds_base stays on base.v1
    base = load_spec(REPO / "experiments" / "configs" / "ds_base.yaml")
    core = load_spec(REPO / "experiments" / "configs" / "ds_core.yaml")
    assert core.name == "ds_core"
    assert str(core.feature_set) == "core.v2"
    assert str(base.feature_set) == "base.v1"
    same = base.model_dump(exclude={"name", "feature_set"})
    assert core.model_dump(exclude={"name", "feature_set"}) == same
    # its feature set exists and every timeframe it reads is a context timeframe of the spec
    cfg = load_config("research", config_dir=REPO / "config")
    needs = warmup_bars(cfg, core.feature_set)
    assert set(needs) - {"base"} == {tf.value for tf in core.context_timeframes}
