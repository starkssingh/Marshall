"""`xq research eda` end to end on a synthetic dataset: the report is deterministic, every file is
a run artifact, the discovery window is enforced, and the admission list reaches a configuration
directory only from a confirmatory run. Synthetic data only (ADR 0035): no EDA report is ever
generated here on real or pseudo-real data."""

import json
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.datasets import dataset_spec, validated_pipeline
from helpers.pipeline import REPO, config
from helpers.ticks import dense_ticks, write_mt5
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.core.types import Timeframe
from xq.datasets.builder import DatasetRef, build_dataset
from xq.research.eda.data import load_eda_inputs
from xq.research.reports import DiscoveryWindowError
from xq.tracking import registry

HYPOTHESIS = "H-0900"
#: A trading-day start inside the synthetic data: the discovery window of the enforcement tests.
DISCOVERY_END = "2024-03-12T21:00:00Z"
SECTIONS = ["overview", "horizons"]


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("eda")


@pytest.fixture(scope="module")
def cfg(root: Path) -> AppConfig:
    return config(root)


@pytest.fixture(scope="module")
def engine(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    ticks_dir = tmp_path_factory.mktemp("eda_ticks")
    ticks = dense_ticks("2024-03-03 22:00", "2024-03-23 00:00", seed=43, mean_interval_s=10)
    write_mt5(ticks, ticks_dir / "XAUUSD_three_weeks.csv")
    engine = validated_pipeline(cfg, ticks_dir)
    registry.add_hypothesis_version(
        engine, HYPOTHESIS, title="EDA test", family_id="eda", yaml_text="x\n"
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def dataset(cfg: AppConfig, engine: Engine) -> DatasetRef:
    spec = dataset_spec(
        start="2024-03-05T00:00:00Z",
        end="2024-03-22T12:00:00Z",
        context_timeframes=["1h", "4h", "1d"],
    )
    return build_dataset(cfg, engine, spec, git_sha="t")


def invoke(root: Path, *args: str, overrides: tuple[str, ...] = ()) -> tuple[int, str]:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--set",
        f"paths.root={root}",
        "--set",
        f"paths.migrations_dir={REPO / 'migrations'}",
        "--set",
        "logging.console=false",
        "--set",
        "logging.file=null",
        "--set",
        "eda.bootstrap.n_boot=100",
    ]
    for item in overrides:
        common += ["--set", item]
    result = CliRunner().invoke(app, [*common, *args])
    return result.exit_code, result.output


def run_eda_cli(
    root: Path, dataset: DatasetRef, *extra: str, overrides: tuple[str, ...] = ()
) -> Path:
    code, output = invoke(
        root,
        "research",
        "eda",
        "--dataset",
        dataset.dataset_id,
        "--hypothesis",
        HYPOTHESIS,
        "--seed",
        "3",
        "--exploratory",
        *extra,
        overrides=overrides,
    )
    assert code == 0, output
    return Path(output.strip().splitlines()[-1].removeprefix("report: "))


@pytest.fixture(scope="module")
def reports(root: Path, dataset: DatasetRef, engine: Engine) -> tuple[Path, Path]:
    return run_eda_cli(root, dataset), run_eda_cli(root, dataset)


def test_report_is_deterministic(reports: tuple[Path, Path]) -> None:
    first, second = reports
    assert first != second
    manifest = json.loads((first / "manifest.json").read_text())["files"]
    assert manifest == json.loads((second / "manifest.json").read_text())["files"]
    for relative in manifest:
        assert (first / relative).read_bytes() == (second / relative).read_bytes()
    runs = [json.loads((d / "run.json").read_text())["run_id"] for d in reports]
    assert runs == [first.name, second.name]


def test_report_has_every_section_and_its_provenance(
    reports: tuple[Path, Path], dataset: DatasetRef
) -> None:
    first = reports[0]
    manifest = json.loads((first / "manifest.json").read_text())["files"]
    assert {f"{s}.md" for s in SECTIONS} <= set(manifest)
    assert "admission.yaml" in manifest
    metadata = json.loads((first / "metadata.json").read_text())
    assert metadata["dataset_id"] == dataset.dataset_id
    assert metadata["discovery_window"]["start"] == "2024-03-05 00:00:00+00:00"
    assert metadata["cost_basis"] == "screening, placeholder costs"
    assert {"git_sha", "app_config_hash", "eda_config_hash", "seed"} <= set(metadata)
    horizons = (first / "horizons.md").read_text()
    assert "screening, placeholder costs" in horizons


def test_every_report_file_is_a_run_artifact(reports: tuple[Path, Path], engine: Engine) -> None:
    first = reports[0]
    manifest = json.loads((first / "manifest.json").read_text())["files"]
    artifacts = registry.list_artifacts(engine, first.name)
    paths = {Path(a.path).relative_to(first).as_posix() for a in artifacts}
    assert paths == {*manifest, "manifest.json", "run.json"}
    assert {a.kind for a in artifacts} == {"eda_report"}
    metrics = {m.name for m in registry.get_metrics(engine, first.name)}
    assert "eda/horizons/1d/cost_to_vol" in metrics
    run = registry.get_run(engine, first.name)
    assert (run.kind, run.confirmatory, run.dataset_id) == ("eda", False, first.parent.name)


def test_only_discovery_data_is_read(cfg: AppConfig, dataset: DatasetRef, engine: Engine) -> None:
    fixed = config(cfg.paths.root, **{"eda.discovery.end": DISCOVERY_END})
    inputs = load_eda_inputs(fixed, dataset.dataset_id, [Timeframe.M1, Timeframe.H1, Timeframe.D1])
    end = pd.Timestamp(DISCOVERY_END)
    assert inputs.window.end == end
    for frame in inputs.bars.values():
        assert len(frame)
        assert (frame["available_at_utc"] <= end).all()
    assert inputs.bars[Timeframe.H1]["available_at_utc"].max() == end
    with pytest.raises(DiscoveryWindowError, match="refuses post-discovery data"):
        load_eda_inputs(fixed, dataset.dataset_id, [Timeframe.H1], end="2024-03-13T21:00:00Z")


def test_cli_refuses_post_discovery_data(root: Path, dataset: DatasetRef, engine: Engine) -> None:
    fixed = (f"eda.discovery.end={DISCOVERY_END}",)
    code, output = invoke(
        root,
        "research",
        "eda",
        "--dataset",
        dataset.dataset_id,
        "--hypothesis",
        HYPOTHESIS,
        "--exploratory",
        "--end",
        "2024-03-15T21:00:00Z",
        overrides=fixed,
    )
    assert code == 2
    assert "refuses post-discovery data" in output
    report = run_eda_cli(root, dataset, overrides=fixed)
    coverage = pd.read_csv(report / "tables" / "overview-coverage.csv")
    last = pd.to_datetime(coverage["last_bar"], utc=True)
    assert (last < pd.Timestamp(DISCOVERY_END)).all()


def test_admission_is_refused_from_an_exploratory_run(
    root: Path, reports: tuple[Path, Path], tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for path in (REPO / "config").rglob("*.yaml"):
        target = config_dir / path.relative_to(REPO / "config")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    result = CliRunner().invoke(
        app,
        [
            "--config-dir",
            str(config_dir),
            "--set",
            f"paths.root={root}",
            "research",
            "admit-horizons",
            "--report",
            str(reports[0]),
        ],
    )
    assert result.exit_code == 2
    assert "exploratory" in result.output
    assert not (config_dir / "horizons.yaml").exists()
    assert not (REPO / "config" / "horizons.yaml").exists()
