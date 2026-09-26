"""Layered, typed, frozen application configuration (ARCH-003).

Layers, from lowest to highest precedence:

1. ``config/base.yaml`` plus the section files next to it (``instruments/<id>.yaml``,
   ``sessions.yaml``)
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
import re
from collections.abc import Mapping
from datetime import UTC, datetime, time
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
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

from xq.core.errors import ClockConventionError, ConfigError
from xq.core.time import ClockConvention

ENV_PREFIX = "XQ_"
ENV_NESTED_DELIMITER = "__"
CONFIG_DIR_ENV = "XQ_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("config")
BASE_FILE = "base.yaml"
SECRETS_SECTION = "secrets"
# Sections kept in their own files: one YAML per entry in a directory (keyed by file stem).
FRAGMENT_DIRS = {"instruments": "instruments"}
# Sections kept in a single YAML file next to base.yaml.
FRAGMENT_FILES = {"sessions": "sessions.yaml"}

_HH_MM = re.compile(r"^(?P<h>[01]\d|2[0-3]):(?P<m>[0-5]\d)(:(?P<s>[0-5]\d))?$")
_MONTH_DAY = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def _parse_hh_mm(value: object) -> time:
    # YAML 1.1 reads an unquoted 17:00 as the integer 1020 (base-60), which pydantic would then
    # accept as 00:17:00. Only quoted "HH:MM" strings are accepted.
    if isinstance(value, time):
        return value
    if not isinstance(value, str) or (match := _HH_MM.match(value)) is None:
        raise ValueError(f'local times must be quoted "HH:MM" strings, got {value!r}')
    return time(int(match["h"]), int(match["m"]), int(match["s"] or 0))


def _check_time_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA time zone {value!r}") from exc
    return value


def _check_month_day(value: str) -> str:
    if not _MONTH_DAY.match(value):
        raise ValueError(f'expected a "MM-DD" string, got {value!r}')
    return value


LocalTime = Annotated[time, BeforeValidator(_parse_hh_mm)]
TimeZoneName = Annotated[str, AfterValidator(_check_time_zone)]
MonthDay = Annotated[str, AfterValidator(_check_month_day)]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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

    time: LocalTime = time(17, 0)
    tz: TimeZoneName = "America/New_York"


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


class MarketHoursConfig(FrozenModel):
    """When the venue quotes, in its local time zone (DATA-002).

    Trading day D runs from 17:00 New York on D-1 to 17:00 New York on D (a fixed convention).
    `open` and `close` are placed at whichever calendar date puts them inside that interval, so
    with the defaults the market opens at 18:00 on D-1 and closes at 17:00 on D. `week_open`
    replaces `open` when the previous calendar day is not a trading weekday (Sunday opens).
    """

    tz: TimeZoneName = "America/New_York"
    open: LocalTime
    close: LocalTime
    week_open: LocalTime
    trading_weekdays: list[Weekday]


class HolidayConfig(FrozenModel):
    """Holiday rules. US holidays come from the `holidays` package's financial calendar."""

    us_calendar: str = "NYSE"
    uk_country: str = "GB"
    uk_subdiv: str = "ENG"
    closed: list[str]
    early_close_time: LocalTime
    early_close_dates: list[MonthDay] = []


class SessionWindow(FrozenModel):
    """A trading session in local time on the trading day's calendar date."""

    tz: TimeZoneName
    open: LocalTime
    close: LocalTime

    @model_validator(mode="after")
    def _check_order(self) -> SessionWindow:
        if self.open >= self.close:
            raise ValueError("session open must be before close in local time")
        return self


class EventAnchor(FrozenModel):
    """A recurring market event at a local time on the trading day's calendar date."""

    tz: TimeZoneName
    time: LocalTime
    skip_on: list[Literal["us_holiday", "uk_holiday"]] = []
    skip_dates: list[MonthDay] = []
    require_open: bool = True


class SessionsConfig(FrozenModel):
    """Calendar, sessions and event anchors (``config/sessions.yaml``)."""

    market: MarketHoursConfig
    holidays: HolidayConfig
    sessions: dict[str, SessionWindow]
    overlaps: dict[str, list[str]] = {}
    event_anchors: dict[str, EventAnchor] = {}

    @model_validator(mode="after")
    def _check_overlaps(self) -> SessionsConfig:
        for name, members in self.overlaps.items():
            unknown = [m for m in members if m not in self.sessions]
            if len(members) < 2 or unknown:
                raise ValueError(f"overlap {name!r} needs two or more known sessions: {members}")
        return self


DroppableRule = Literal["DUP_EXACT", "NONPOSITIVE", "CROSSED"]


class SpreadOutlierRule(FrozenModel):
    """SPREAD_OUTLIER: spread above `multiple` x the median of the previous `window_ticks`."""

    window_ticks: int = Field(gt=0)
    min_periods: int = Field(gt=0)
    multiple: float = Field(gt=1)


class SpikeRule(FrozenModel):
    """SPIKE: |mid log return| / robust scale above `z_threshold`, reverting within a few ticks.

    The robust scale is 1.4826 x the median absolute return of the previous `window_ticks`
    returns, floored at `min_scale_bps`. A candidate is confirmed if, within `reversal_ticks`
    later ticks, the mid comes back by at least `reversal_fraction` of the jump.
    """

    window_ticks: int = Field(gt=0)
    min_periods: int = Field(gt=0)
    z_threshold: float = Field(gt=0)
    reversal_ticks: int = Field(gt=0)
    reversal_fraction: float = Field(gt=0, le=1)
    min_scale_bps: float = Field(gt=0)


class StaleRule(FrozenModel):
    """STALE: the quote repeats unchanged for longer than `seconds`."""

    seconds: float = Field(gt=0)


class CleaningConfig(FrozenModel):
    """Versioned, non-destructive cleaning rules (DATA-007).

    Every rule flags. Only exact duplicates, non-positive and crossed quotes may additionally be
    dropped, and only if listed in `drop`.
    """

    version: str
    drop: list[DroppableRule] = []
    log_flag_actions: bool = True
    spread_outlier: SpreadOutlierRule
    spike: SpikeRule
    stale: StaleRule


BarExcludableFlag = Literal[
    "MISSING_QUOTE",
    "NONPOSITIVE",
    "CROSSED",
    "DUP_EXACT",
    "DUP_TS_DIFF_PRICE",
    "CLOSED_MARKET",
    "STALE",
    "SPREAD_OUTLIER",
    "TS_DST_AMBIGUOUS",
    "TS_DST_NONEXISTENT",
    "TS_OUT_OF_ORDER",
]


class BarsConfig(FrozenModel):
    """Bar construction settings (DATA-008).

    `exclude_flags` lists tick flags whose ticks do not enter bar prices. Only flags a live system
    could know when the tick arrives are allowed: ``SPIKE`` is confirmed by later ticks, so
    excluding it would let bars use future information.
    """

    version: str
    publication_latency_ms: int = Field(ge=0)
    exclude_flags: list[BarExcludableFlag]

    @field_validator("exclude_flags", mode="before")
    @classmethod
    def _refuse_non_causal(cls, value: object) -> object:
        if isinstance(value, list) and "SPIKE" in value:
            raise ValueError(
                "SPIKE cannot exclude ticks from bars: it is confirmed by later ticks, so bars "
                "would use information a live system does not have yet"
            )
        return value


class SourceConfig(FrozenModel):
    """A declared market-data source (DATA-003). The clock convention is part of its identity."""

    adapter: Literal["mt5_ticks"]
    vendor: str
    feed_type: Literal["broker_ticks", "vendor_ticks", "vendor_bars"]
    venue: str
    price_type: Literal["bid_ask_ticks", "bars"]
    clock: str
    instrument: str
    file_patterns: list[str] = ["*.csv"]
    encoding: str | None = None
    notes: str = ""

    @field_validator("clock")
    @classmethod
    def _canonical_clock(cls, value: str) -> str:
        try:
            return str(ClockConvention.parse(value))
        except ClockConventionError as exc:
            raise ValueError(str(exc)) from exc

    def clock_convention(self) -> ClockConvention:
        """The parsed clock convention."""
        return ClockConvention.parse(self.clock)


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
    sessions: SessionsConfig | None = None
    sources: dict[str, SourceConfig] = {}
    cleaning: CleaningConfig | None = None
    bars: BarsConfig | None = None
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

    @model_validator(mode="after")
    def _check_source_instruments(self) -> AppConfig:
        for source_id, source in self.sources.items():
            if source.instrument not in self.instruments:
                raise ValueError(
                    f"source {source_id!r} refers to unknown instrument {source.instrument!r}"
                )
        return self

    def source(self, source_id: str) -> SourceConfig:
        """Return the configuration of `source_id`."""
        try:
            return self.sources[source_id]
        except KeyError:
            known = ", ".join(sorted(self.sources)) or "none"
            raise ConfigError(f"unknown source {source_id!r}; configured: {known}") from None

    def cleaning_config(self) -> CleaningConfig:
        """Return the cleaning rules; raise if they are not configured."""
        if self.cleaning is None:
            raise ConfigError("no cleaning configuration (cleaning: in config/base.yaml)")
        return self.cleaning

    def bars_config(self) -> BarsConfig:
        """Return the bar construction settings; raise if they are not configured."""
        if self.bars is None:
            raise ConfigError("no bars configuration (bars: in config/base.yaml)")
        return self.bars

    def sessions_config(self) -> SessionsConfig:
        """Return the calendar and session configuration; raise if it is not configured."""
        if self.sessions is None:
            raise ConfigError("no sessions configuration (config/sessions.yaml) was loaded")
        return self.sessions

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
    for section, filename in FRAGMENT_FILES.items():
        path = directory / filename
        if path.is_file():
            layer[section] = _read_yaml(path)
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
