"""ARCH-006: CLI skeleton."""

import json
from pathlib import Path

from typer.testing import CliRunner

import xq
from xq.cli.main import app

REPO_CONFIG = str(Path(__file__).resolve().parents[3] / "config")
runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"xq {xq.__version__}"


def test_help_lists_planned_command_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("config", "dataset", "exp", "baselines", "research", "registry", "gate"):
        assert group in result.stdout


def test_config_show_masks_secrets_and_applies_overrides() -> None:
    result = runner.invoke(
        app,
        [
            "--config-dir",
            REPO_CONFIG,
            "--profile",
            "research",
            "--set",
            "logging.level=ERROR",
            "config",
            "show",
            "--format",
            "json",
        ],
        env={"XQ_SECRETS__BROKER_API_KEY": "topsecret-value"},
    )
    assert result.exit_code == 0, result.output
    assert "topsecret-value" not in result.output
    payload = json.loads(result.stdout)
    assert payload["config"]["profile"] == "research"
    assert payload["config"]["logging"]["level"] == "ERROR"
    assert payload["config"]["secrets"]["broker_api_key"] == "**********"
    assert len(payload["config_hash"]) == 16


def test_config_show_yaml_includes_hash_header() -> None:
    result = runner.invoke(app, ["--config-dir", REPO_CONFIG, "config", "show"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("# profile: dev\n# config_hash: ")


def test_unknown_profile_is_a_clean_usage_error() -> None:
    result = runner.invoke(app, ["--config-dir", REPO_CONFIG, "-p", "nope", "config", "show"])
    assert result.exit_code == 2
    assert "unknown profile 'nope'" in result.stderr
    assert "Traceback" not in result.output


def test_db_upgrade_and_current(tmp_path: Path) -> None:
    migrations = str(Path(REPO_CONFIG).parent / "migrations")
    common = [
        "--config-dir",
        REPO_CONFIG,
        "--set",
        f"paths.root={tmp_path}",
        "--set",
        f"paths.migrations_dir={migrations}",
    ]
    result = runner.invoke(app, [*common, "db", "current"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["current: none", "head: 0001"]

    result = runner.invoke(app, [*common, "db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "revision 0001" in result.stdout
    assert (tmp_path / "data" / "metadata.sqlite").is_file()

    result = runner.invoke(app, [*common, "db", "current"])
    assert result.stdout.splitlines() == ["current: 0001", "head: 0001"]
