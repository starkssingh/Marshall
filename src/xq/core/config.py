"""Layered, typed, frozen application configuration (ARCH-003).

Layers, from lowest to highest precedence:

1. ``config/base.yaml``
2. ``config/<profile>.yaml`` (for example ``dev``, ``research``, ``paper``, ``prod``)
3. environment variables prefixed ``XQ_``; nested keys are separated by ``__``,
   e.g. ``XQ_LOGGING__LEVEL=DEBUG``
4. explicit overrides, e.g. from the CLI (``--set logging.level=DEBUG``)

The result is a frozen `AppConfig` that is passed explicitly; there is no module-level config
object. Secrets are typed as `SecretStr`, may only come from environment variables, and are
excluded from `config_hash`.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from xq.core.errors import ConfigError

ENV_PREFIX = "XQ_"
ENV_NESTED_DELIMITER = "__"
CONFIG_DIR_ENV = "XQ_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("config")
BASE_FILE = "base.yaml"
SECRETS_SECTION = "secrets"


class FrozenModel(BaseModel):
    """Base for configuration sections: immutable and strict about unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectConfig(FrozenModel):
    """Project identity."""

    name: str = "xq"


class PathsConfig(FrozenModel):
    """Filesystem locations. Relative paths are resolved against `root` when used."""

    root: Path = Path(".")
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")
    logs_dir: Path = Path("logs")
    migrations_dir: Path = Path("migrations")

    def resolve(self, path: Path) -> Path:
        """Return `path` as an absolute path, interpreting relative paths against `root`."""
        return (path if path.is_absolute() else self.root / path).resolve()


class DatabaseConfig(FrozenModel):
    """Metadata database. `url=None` means SQLite at ``<data_dir>/metadata.sqlite``."""

    url: str | None = None


class LoggingConfig(FrozenModel):
    """Structured logging settings (see `xq.core.logging`)."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console: bool = True
    console_format: Literal["json", "console"] = "json"
    file: Path | None = Path("logs/xq.jsonl")


class VaultConfig(FrozenModel):
    """The untouchable holdout: all data at or after `start` (UTC)."""

    start: AwareDatetime

    @field_validator("start")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class SecretsConfig(FrozenModel):
    """Credentials. Only ever supplied through ``XQ_SECRETS__*`` environment variables."""

    broker_api_key: SecretStr | None = None
    data_api_key: SecretStr | None = None


class AppConfig(BaseSettings):
    """The fully resolved application configuration. Build it with `load_config`."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        case_sensitive=False,
        frozen=True,
        extra="forbid",
    )

    profile: str
    project: ProjectConfig = ProjectConfig()
    paths: PathsConfig = PathsConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    vault: VaultConfig
    secrets: SecretsConfig = SecretsConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # `load_config` merges every layer itself so the precedence is explicit and testable;
        # constructing AppConfig only validates what it is given.
        return (init_settings,)

    def database_url(self) -> str:
        """Return the metadata database URL, defaulting to SQLite under the data directory."""
        if self.database.url is not None:
            return self.database.url
        return f"sqlite:///{self.paths.resolve(self.paths.data_dir) / 'metadata.sqlite'}"


def load_config(
    profile: str,
    overrides: Mapping[str, Any] | None = None,
    *,
    config_dir: Path | None = None,
) -> AppConfig:
    """Resolve the configuration for `profile`.

    Args:
        profile: Name of the profile file in the config directory (without ``.yaml``).
        overrides: Highest-precedence values, either nested mappings or dotted keys
            (``{"logging.level": "DEBUG"}``).
        config_dir: Directory holding the YAML files. Defaults to ``$XQ_CONFIG_DIR`` or ``config``.

    Raises:
        ConfigError: if a file is missing or malformed, a secret is set outside the environment,
            or the merged values fail validation.
    """
    directory = _config_dir(config_dir)
    file_layers = [_read_yaml(directory / BASE_FILE), _read_profile(directory, profile)]
    for path, layer in zip((BASE_FILE, f"{profile}.yaml"), file_layers, strict=True):
        _reject_secrets(layer, where=f"config file {path}")

    override_layer = nest_dotted(overrides or {})
    _reject_secrets(override_layer, where="overrides")

    env_layer = dict(EnvSettingsSource(AppConfig)())
    env_layer.pop("profile", None)  # the profile is chosen by the caller, not the environment

    merged: dict[str, Any] = {}
    for layer in (*file_layers, env_layer, override_layer):
        merged = deep_merge(merged, layer)
    merged["profile"] = profile

    try:
        return AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration for profile {profile!r}:\n{exc}") from exc


def config_hash(cfg: AppConfig) -> str:
    """Return a stable 16-hex-digit hash of the resolved configuration, excluding secrets.

    The hash is computed over canonical JSON (sorted keys, no whitespace), so it does not depend on
    key order in the YAML files.
    """
    payload = cfg.model_dump(mode="json", exclude={SECRETS_SECTION})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def config_as_dict(cfg: AppConfig) -> dict[str, Any]:
    """Return the configuration as JSON-compatible data with secrets masked."""
    return cfg.model_dump(mode="json")


def parse_override(assignment: str) -> tuple[str, Any]:
    """Parse ``dotted.key=value``; the value is read as a YAML scalar (so ``true``, ``3`` work)."""
    key, sep, raw = assignment.partition("=")
    if not sep or not key.strip():
        raise ConfigError(f"override must look like 'section.key=value', got {assignment!r}")
    return key.strip(), yaml.safe_load(raw) if raw.strip() else ""


def nest_dotted(values: Mapping[str, Any]) -> dict[str, Any]:
    """Expand dotted keys into nested dictionaries (``{"a.b": 1} -> {"a": {"b": 1}}``)."""
    nested: dict[str, Any] = {}
    for key, value in values.items():
        branch: Any = nest_dotted(value) if isinstance(value, Mapping) else value
        for part in reversed(key.split(".")):
            branch = {part: branch}
        nested = deep_merge(nested, branch)
    return nested


def deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge `update` into a copy of `base`: mappings merge, other values replace."""
    merged = dict(base)
    for key, value in update.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _config_dir(config_dir: Path | None) -> Path:
    directory = config_dir or Path(os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR))
    if not directory.is_dir():
        raise ConfigError(f"config directory not found: {directory}")
    return directory


def _read_profile(directory: Path, profile: str) -> dict[str, Any]:
    path = directory / f"{profile}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in directory.glob("*.yaml") if p.name != BASE_FILE)
        raise ConfigError(f"unknown profile {profile!r}; available: {', '.join(available)}")
    return _read_yaml(path)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _reject_secrets(layer: Mapping[str, Any], *, where: str) -> None:
    section = layer.get(SECRETS_SECTION)
    if isinstance(section, Mapping) and any(v is not None for v in section.values()):
        raise ConfigError(
            f"secrets may only be set through {ENV_PREFIX}{SECRETS_SECTION.upper()}"
            f"{ENV_NESTED_DELIMITER}* environment variables, not in {where}"
        )
