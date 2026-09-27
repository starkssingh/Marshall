"""EXP-002: hypothesis pre-registration with locked hashes and visible versions."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.pipeline import REPO, config
from xq.cli.main import app
from xq.core.config import AppConfig
from xq.core.errors import ConfigError
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.hypotheses import is_registered, load_hypothesis, register_hypothesis
from xq.tracking.registry import HypothesisStatus, get_hypothesis, hypothesis_text

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


def write(directory: Path, **changes: Any) -> Path:
    data = yaml.safe_load(TEMPLATE.read_text())
    data.update({"id": "H-0001", **changes})
    path = directory / f"{data['id']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_template_is_complete_but_not_registrable(cfg: AppConfig) -> None:
    with pytest.raises(ConfigError, match="pattern"):
        load_hypothesis(TEMPLATE, cfg)  # id H-XXXX is a placeholder
    data = yaml.safe_load(TEMPLATE.read_text())
    assert {"statement", "falsification_criteria", "trial_budget", "slices"} <= set(data)


def test_registration_locks_the_text_and_edits_create_versions(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    path = write(tmp_path)
    first = register_hypothesis(cfg, engine, path)
    assert (first.hypothesis_id, first.version) == ("H-0001", 1)
    assert is_registered(engine, path)
    assert register_hypothesis(cfg, engine, path) == first  # unchanged file: no new version

    path.write_text(path.read_text().replace("trial_budget: 20", "trial_budget: 50"))
    assert not is_registered(engine, path)
    second = register_hypothesis(cfg, engine, path)
    assert second.version == 2
    assert get_hypothesis(engine, "H-0001", 1).status is HypothesisStatus.SUPERSEDED
    assert "trial_budget: 20" in hypothesis_text(engine, "H-0001", 1)
    assert "trial_budget: 50" in hypothesis_text(engine, "H-0001", 2)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"falsification_criteria": []}, "at least 1"),
        ({"trial_budget": 0}, "greater than 0"),
        ({"timeframe": "7m"}, "timeframe"),
        (
            {"discovery_window": {"start": "2021-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"}},
            "discovery window must end before",
        ),
        (
            {"evaluation_window": {"start": "2023-09-24T21:00:00Z", "end": "2026-01-01T00:00:00Z"}},
            "after vault.start",
        ),
        (
            {"evaluation_window": {"start": "2023-09-24T21:00:00Z", "end": "2023-09-24T21:00:00Z"}},
            "must end after it starts",
        ),
        (
            {"discovery_window": {"start": "2021-01-01T00:00:00", "end": "2022-01-01T00:00:00Z"}},
            "timezone",
        ),
        ({"extra_field": 1}, "Extra inputs"),
    ],
)
def test_invalid_hypotheses_are_refused(
    cfg: AppConfig, tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_hypothesis(write(tmp_path, **changes), cfg)


@pytest.mark.parametrize("family", ["momentum", "baselines", "descriptive_x", "descriptives"])
def test_a_zero_trial_budget_is_refused_outside_the_descriptive_family(
    cfg: AppConfig, tmp_path: Path, family: str
) -> None:
    with pytest.raises(ConfigError, match="greater than 0 unless the family is 'descriptive'"):
        load_hypothesis(write(tmp_path, family=family, trial_budget=0), cfg)
    doc, _ = load_hypothesis(write(tmp_path, family=family, trial_budget=1), cfg)
    assert doc.trial_budget == 1


def test_a_descriptive_hypothesis_may_have_a_zero_trial_budget(
    cfg: AppConfig, engine: Engine, tmp_path: Path
) -> None:
    doc, _ = load_hypothesis(write(tmp_path, family="descriptive", trial_budget=0), cfg)
    assert (doc.family, doc.trial_budget) == ("descriptive", 0)
    ref = register_hypothesis(cfg, engine, tmp_path / "H-0001.yaml")
    assert get_hypothesis(engine, ref.hypothesis_id).family_id == "descriptive"
    with pytest.raises(ConfigError, match="greater than or equal to 0"):
        load_hypothesis(write(tmp_path, family="descriptive", trial_budget=-1), cfg)


def test_file_name_must_match_the_id(cfg: AppConfig, tmp_path: Path) -> None:
    path = write(tmp_path)
    renamed = path.rename(tmp_path / "H-0002.yaml")
    with pytest.raises(ConfigError, match=r"must be named H-0001\.yaml"):
        load_hypothesis(renamed, cfg)


def test_cli_register_and_list(tmp_path: Path) -> None:
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
    path = write(tmp_path)
    runner = CliRunner()
    first = runner.invoke(app, [*common, "exp", "register", str(path)])
    assert first.exit_code == 0, first.output
    assert "H-0001 version 1 registered" in first.stdout
    again = runner.invoke(app, [*common, "exp", "register", str(path)])
    assert "H-0001 version 1 unchanged" in again.stdout
    listed = runner.invoke(app, [*common, "exp", "hypotheses"])
    assert "H-0001\tv1\tactive\tmomentum" in listed.stdout
    bad = runner.invoke(app, [*common, "exp", "register", str(TEMPLATE)])
    assert bad.exit_code == 2
