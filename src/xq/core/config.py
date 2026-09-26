"""Layered, typed, frozen application configuration (ARCH-003).

Layers, from lowest to highest precedence:

1. ``config/base.yaml`` plus the section files next to it (``instruments/<id>.yaml``, ...)
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
from decimal import ROUND_FLOOR, Decimal
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
    model_validator,
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
# Sections kept in their own files: one YAML per entry in a directory (keyed by file stem).
FRAGMENT_DIRS = {"instruments": "instruments"}


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


class RolloverConfig(FrozenModel):
    """Daily rollover (financing) time in its local time zone."""

    time: str = "17:00"
    tz: str = "America/New_York"


class VenueOverride(FrozenModel):
    """Contract terms that differ at a particular venue (broker)."""

    tick_size: Decimal | None = None
    contract_size: Decimal | None = None
    lot_step: Decimal | None = None
    min_lot: Decimal | None = None
    max_lot: Decimal | None = None


class InstrumentSpec(FrozenModel):
    """Contract terms of a tradable instrument (DATA-001).

    Quantities are `Decimal` so lot rounding is exact. `contract_size` is units of the base asset
    per lot (troy ounces for XAUUSD), so a price move of 1 quote-currency unit on one lot changes
    P&L by `contract_size` units of the quote currency.
    """

    symbol: str
    base_ccy: str
    quote_ccy: str
    tick_size: Decimal
    contract_size: Decimal
    lot_step: Decimal
    min_lot: Decimal
    max_lot: Decimal
    rollover: RolloverConfig = RolloverConfig()
    venues: dict[str, VenueOverride] = {}

    @model_validator(mode="after")
    def _check_terms(self) -> InstrumentSpec:
        for name in ("tick_size", "contract_size", "lot_step", "min_lot", "max_lot"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.min_lot > self.max_lot:
            raise ValueError("min_lot must not exceed max_lot")
        for name in ("min_lot", "max_lot"):
            if getattr(self, name) % self.lot_step != 0:
                raise ValueError(f"{name} must be a multiple of lot_step")
        return self

    def for_venue(self, venue: str | None) -> InstrumentSpec:
        """Return the spec with `venue`'s overrides applied (unchanged if it has none)."""
        override = self.venues.get(venue) if venue is not None else None
        if override is None:
            return self
        changes = override.model_dump(exclude_none=True)
        return InstrumentSpec.model_validate({**self.model_dump(), **changes})

    def round_lots(self, requested: Decimal | float | str) -> Decimal:
        """Round a requested size down to the lot step, capped at `max_lot`.

        Returns 0 when the rounded size is below `min_lot` (the trade cannot be placed). Rounding is
        always down, so a size never exceeds what risk sizing asked for.
        """
        lots = Decimal(str(requested))
        if lots < 0:
            raise ValueError(f"lot size must be non-negative, got {requested!r}")
        capped = min(lots, self.max_lot)
        steps = (capped / self.lot_step).to_integral_value(rounding=ROUND_FLOOR)
        rounded = steps * self.lot_step
        return rounded if rounded >= self.min_lot else Decimal(0)

    def value_per_price_unit(self, lots: Decimal) -> Decimal:
        """P&L in quote currency for a 1.0 quote-currency price move on `lots` lots."""
        return self.contract_size * lots

    def notional(self, price: Decimal, lots: Decimal) -> Decimal:
        """Position value in quote currency."""
        return price * self.contract_size * lots

    def price_to_ticks(self, price_distance: Decimal) -> Decimal:
        """Express a price distance in ticks."""
        return price_distance / self.tick_size


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
    instruments: dict[str, InstrumentSpec] = {}
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

    def instrument(self, instrument_id: str, venue: str | None = None) -> InstrumentSpec:
        """Return the spec for `instrument_id`, with `venue` overrides applied if given."""
        try:
            spec = self.instruments[instrument_id]
        except KeyError:
            known = ", ".join(sorted(self.instruments)) or "none"
            raise ConfigError(
                f"unknown instrument {instrument_id!r}; configured: {known}"
            ) from None
        return spec.for_venue(venue)

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
    base_layer = deep_merge(_read_fragments(directory), _read_yaml(directory / BASE_FILE))
    file_layers = [base_layer, _read_profile(directory, profile)]
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


def _read_fragments(directory: Path) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for section, dirname in FRAGMENT_DIRS.items():
        folder = directory / dirname
        if folder.is_dir():
            layer[section] = {path.stem: _read_yaml(path) for path in sorted(folder.glob("*.yaml"))}
    return layer


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
