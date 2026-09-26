"""ARCH-003: layered typed configuration."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from xq.core.config import (
    AppConfig,
    config_as_dict,
    config_hash,
    load_config,
    nest_dotted,
    parse_override,
)
from xq.core.errors import ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"

BASE = """
logging:
  level: INFO
  console: true
  file: null
vault:
  start: "2025-09-25T21:00:00Z"
"""


def _write_config(tmp_path: Path, base: str = BASE, **profiles: str) -> Path:
    directory = tmp_path / "config"
    directory.mkdir(parents=True)
    (directory / "base.yaml").write_text(base)
    for name, text in profiles.items():
        (directory / f"{name}.yaml").write_text(text)
    return directory


@pytest.mark.parametrize("profile", ["dev", "research", "paper", "prod"])
def test_repository_profiles_validate(profile: str) -> None:
    cfg = load_config(profile, config_dir=REPO_CONFIG)
    assert cfg.profile == profile
    assert cfg.vault.start == datetime(2025, 9, 25, 21, tzinfo=UTC)


def test_precedence_base_profile_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_config(
        tmp_path,
        dev="logging:\n  level: DEBUG\n  console_format: console\n",
    )
    cfg = load_config("dev", config_dir=directory)
    assert cfg.logging.level == "DEBUG"  # profile beats base
    assert cfg.logging.console is True  # untouched base value survives the merge

    monkeypatch.setenv("XQ_LOGGING__LEVEL", "WARNING")
    monkeypatch.setenv("XQ_LOGGING__CONSOLE", "false")
    cfg = load_config("dev", config_dir=directory)
    assert cfg.logging.level == "WARNING"  # environment beats profile
    assert cfg.logging.console is False
    assert cfg.logging.console_format == "console"

    cfg = load_config("dev", {"logging.level": "ERROR"}, config_dir=directory)
    assert cfg.logging.level == "ERROR"  # explicit override beats environment
    assert cfg.logging.console is False


def test_environment_cannot_change_the_requested_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_config(tmp_path, dev="", research="")
    monkeypatch.setenv("XQ_PROFILE", "research")
    assert load_config("dev", config_dir=directory).profile == "dev"


def test_config_dir_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _write_config(tmp_path, dev="")
    monkeypatch.setenv("XQ_CONFIG_DIR", str(directory))
    assert load_config("dev").profile == "dev"


def test_config_is_frozen(tmp_path: Path) -> None:
    cfg = load_config("dev", config_dir=_write_config(tmp_path, dev=""))
    with pytest.raises(ValidationError):
        cfg.profile = "prod"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        cfg.logging.level = "DEBUG"  # type: ignore[misc]


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    directory = _write_config(tmp_path, dev="logging:\n  levle: DEBUG\n")
    with pytest.raises(ConfigError, match="levle"):
        load_config("dev", config_dir=directory)


def test_unknown_profile_lists_available_profiles(tmp_path: Path) -> None:
    directory = _write_config(tmp_path, dev="", research="")
    with pytest.raises(ConfigError, match="available: dev, research"):
        load_config("staging", config_dir=directory)


def test_hash_is_stable_under_key_reordering(tmp_path: Path) -> None:
    reordered = """
vault:
  start: "2025-09-25T21:00:00Z"
logging:
  file: null
  console: true
  level: INFO
"""
    first = load_config("dev", config_dir=_write_config(tmp_path / "a", dev=""))
    second = load_config("dev", config_dir=_write_config(tmp_path / "b", reordered, dev=""))
    assert config_hash(first) == config_hash(second)
    assert len(config_hash(first)) == 16


def test_hash_changes_with_values_but_not_with_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_config(tmp_path, dev="")
    baseline = config_hash(load_config("dev", config_dir=directory))
    changed = config_hash(load_config("dev", {"logging.level": "DEBUG"}, config_dir=directory))
    assert changed != baseline

    monkeypatch.setenv("XQ_SECRETS__BROKER_API_KEY", "s3cr3t-value")
    assert config_hash(load_config("dev", config_dir=directory)) == baseline


def test_secrets_are_never_printed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XQ_SECRETS__BROKER_API_KEY", "s3cr3t-value")
    cfg = load_config("dev", config_dir=_write_config(tmp_path, dev=""))

    assert cfg.secrets.broker_api_key is not None
    assert cfg.secrets.broker_api_key.get_secret_value() == "s3cr3t-value"
    for rendering in (repr(cfg), str(cfg), str(config_as_dict(cfg)), cfg.model_dump_json()):
        assert "s3cr3t-value" not in rendering


@pytest.mark.parametrize(
    ("profile_text", "overrides"),
    [
        ("secrets:\n  broker_api_key: abc\n", None),
        ("", {"secrets.broker_api_key": "abc"}),
    ],
)
def test_secrets_outside_environment_are_rejected(
    tmp_path: Path, profile_text: str, overrides: dict[str, str] | None
) -> None:
    directory = _write_config(tmp_path, dev=profile_text)
    with pytest.raises(ConfigError, match="environment variables"):
        load_config("dev", overrides, config_dir=directory)


def test_vault_start_must_be_timezone_aware(tmp_path: Path) -> None:
    naive = BASE.replace('"2025-09-25T21:00:00Z"', '"2025-09-25T21:00:00"')
    with pytest.raises(ConfigError, match="timezone"):
        load_config("dev", config_dir=_write_config(tmp_path, naive, dev=""))


def test_vault_start_is_normalized_to_utc(tmp_path: Path) -> None:
    offset = BASE.replace('"2025-09-25T21:00:00Z"', '"2025-09-25T17:00:00-04:00"')
    cfg = load_config("dev", config_dir=_write_config(tmp_path, offset, dev=""))
    assert cfg.vault.start == datetime(2025, 9, 25, 21, tzinfo=UTC)
    assert cfg.vault.start.utcoffset() is not None


def test_default_database_is_sqlite_under_data_dir(tmp_path: Path) -> None:
    cfg = load_config(
        "dev", {"paths.root": str(tmp_path)}, config_dir=_write_config(tmp_path, dev="")
    )
    assert cfg.database_url() == f"sqlite:///{tmp_path.resolve() / 'data' / 'metadata.sqlite'}"


def test_parse_override_reads_yaml_scalars() -> None:
    assert parse_override("logging.console=false") == ("logging.console", False)
    assert parse_override("a.b=3") == ("a.b", 3)
    assert parse_override("a.b=text") == ("a.b", "text")
    with pytest.raises(ConfigError):
        parse_override("no-equals-sign")


def test_nest_dotted_merges_siblings() -> None:
    assert nest_dotted({"a.b": 1, "a.c": 2, "d": {"e.f": 3}}) == {
        "a": {"b": 1, "c": 2},
        "d": {"e": {"f": 3}},
    }


def test_app_config_ignores_environment_when_constructed_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XQ_LOGGING__LEVEL", "ERROR")
    cfg = AppConfig(profile="x", vault={"start": "2025-09-25T21:00:00Z"})  # type: ignore[arg-type]
    assert cfg.logging.level == "INFO"
