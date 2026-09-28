"""Layered, typed, frozen application configuration (ARCH-003).

Layers, from lowest to highest precedence:

1. ``config/base.yaml`` plus the section files next to it (``instruments/<id>.yaml``,
   ``costs/<model>.yaml``, ``risk/<profile>.yaml``, ``sessions.yaml``, ``quality.yaml``,
   ``targets.yaml``, ``gates.yaml``, ``eda.yaml``, ``stats.yaml``, ``volatility.yaml``,
   ``validation.yaml``)
2. ``config/<profile>.yaml`` (for example ``dev``, ``research``, ``paper``, ``prod``)
3. environment variables prefixed ``XQ_``; nested keys are separated by ``__``,
   e.g. ``XQ_LOGGING__LEVEL=DEBUG``
4. explicit overrides, e.g. from the CLI (``--set logging.level=DEBUG``)

The result is a frozen `AppConfig` that is passed explicitly; there is no module-level config
object. Secrets are typed as `SecretStr`, may only come from environment variables, and are
excluded from `config_hash`. The evidence gates (``gates.yaml``, VAL-007) may only come from their
own file: no other layer can change a threshold.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
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
from xq.core.types import Timeframe

ENV_PREFIX = "XQ_"
ENV_NESTED_DELIMITER = "__"
CONFIG_DIR_ENV = "XQ_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("config")
BASE_FILE = "base.yaml"
SECRETS_SECTION = "secrets"
# Sections kept in their own files: one YAML per entry in a directory (keyed by file stem).
FRAGMENT_DIRS = {"instruments": "instruments", "costs": "costs", "risk": "risk"}
# Sections kept in a single YAML file next to base.yaml.
FRAGMENT_FILES = {
    "sessions": "sessions.yaml",
    "quality": "quality.yaml",
    "targets": "targets.yaml",
    "gates": "gates.yaml",
    "eda": "eda.yaml",
    "stats": "stats.yaml",
    "volatility": "volatility.yaml",
    "validation": "validation.yaml",
}
#: Sections that only their own file may set: no base.yaml key, profile, environment variable or
#: override may change them (evidence gates are fixed before results are seen, ADR 0032).
FILE_ONLY_SECTIONS = {"gates": "gates.yaml"}

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
    #: Research log: one entry appended per closed experiment (EXP-005).
    research_log: Path = Path("docs/research/log.md")

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


class EventWindow(FrozenModel):
    """A window around an event anchor: ``[anchor - before_min, anchor + after_min)``."""

    before_min: int = Field(ge=0)
    after_min: int = Field(ge=0)


class ClockWindow(FrozenModel):
    """A local clock-time window ``[start, end)`` in `tz` on every calendar day (ADR 0026).

    Unlike an `EventWindow` it does not depend on an anchor occurring, so a rollover clock window
    also covers the Sunday reopen, which follows no rollover.
    """

    tz: TimeZoneName
    start: LocalTime
    end: LocalTime

    @model_validator(mode="after")
    def _check_order(self) -> ClockWindow:
        if self.start >= self.end:
            raise ValueError("clock window start must be before its end in local time")
        return self


class SessionsConfig(FrozenModel):
    """Calendar, sessions and event anchors (``config/sessions.yaml``).

    `event_windows` defines the ``in_<name>_window`` dataset columns (DS-007): an `EventWindow`
    around the event anchor of the same name, or a `ClockWindow`.
    """

    market: MarketHoursConfig
    holidays: HolidayConfig
    sessions: dict[str, SessionWindow]
    overlaps: dict[str, list[str]] = {}
    event_anchors: dict[str, EventAnchor] = {}
    event_windows: dict[str, EventWindow | ClockWindow] = {}

    @model_validator(mode="after")
    def _check_overlaps(self) -> SessionsConfig:
        for name, members in self.overlaps.items():
            unknown = [m for m in members if m not in self.sessions]
            if len(members) < 2 or unknown:
                raise ValueError(f"overlap {name!r} needs two or more known sessions: {members}")
        anchored = {n for n, w in self.event_windows.items() if isinstance(w, EventWindow)}
        unknown_windows = sorted(anchored - set(self.event_anchors))
        if unknown_windows:
            raise ValueError(f"event windows for unknown anchors: {unknown_windows}")
        return self


DroppableRule = Literal["DUP_EXACT", "NONPOSITIVE", "CROSSED"]


class SpreadOutlierRule(FrozenModel):
    """SPREAD_OUTLIER: spread above `multiple` x the median of the previous `window_ticks`."""

    window_ticks: int = Field(gt=0)
    min_periods: int = Field(gt=0)
    multiple: float = Field(gt=1)


class SpikeRule(FrozenModel):
    """SPIKE: a whole-quote jump beyond `z_threshold` that reverts within a few ticks (ADR 0008).

    The jump is the common move of bid and ask (same direction, smaller magnitude). Its scale is
    1.4826 x the median absolute mid return of the previous `window_ticks` returns, floored at
    `min_scale_bps`, times sqrt(elapsed time / median tick spacing). A candidate is confirmed if,
    within `reversal_ticks` later ticks, the mid comes back by at least `reversal_fraction` of the
    jump.
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


class CheckThreshold(FrozenModel):
    """Thresholds of one quality check (DQ-001). ``None`` means the level is never reached.

    Status of a measurement: FAIL if ``metric > fail``, else WARN if ``metric > warn``, else PASS.
    A warn threshold of 0 therefore means "warn on any".
    """

    severity: Literal["critical", "major", "minor"]
    unit: str
    warn: float | None
    fail: float | None
    params: dict[str, Any] = {}

    @model_validator(mode="after")
    def _ordered(self) -> CheckThreshold:
        if self.warn is not None and self.fail is not None and self.warn > self.fail:
            raise ValueError("warn threshold must not exceed fail threshold")
        return self


class QualityConfig(FrozenModel):
    """Data-quality settings (``config/quality.yaml``). Thresholds change only with an ADR."""

    active_sessions: list[str]
    top_anomalies: int = Field(default=20, gt=0)
    checks: dict[str, CheckThreshold] = {}


PriceRef = Literal["long", "short", "mid"]


#: A horizon of whole trading days, e.g. ``1d`` (ADR 0032).
TRADING_DAYS_HORIZON = re.compile(r"^(?P<days>[1-9]\d*)[dD]$")
_DAY_UNIT = re.compile(r"\d\s*(days?|d)(?![a-z])", re.IGNORECASE)


def check_horizon_labels(value: list[str]) -> list[str]:
    """Validate trading-time horizon labels (ADR 0026, ADR 0032): positive, unique, and either
    whole trading days (``1d``) or market time without a day unit (``15m``, ``36h``)."""
    for text in value:
        try:
            horizon = pd.Timedelta(text)
        except ValueError as exc:
            raise ValueError(f"invalid horizon {text!r}") from exc
        if horizon <= pd.Timedelta(0):
            raise ValueError(f"horizon {text!r} must be positive")
        if not TRADING_DAYS_HORIZON.match(text) and _DAY_UNIT.search(text):
            raise ValueError(
                f"horizon {text!r} mixes days with other units; write whole trading days "
                "('1d') or market hours and minutes ('36h')"
            )
    if len(set(value)) != len(value):
        raise ValueError("horizons must be unique")
    return value


class TargetSetConfig(FrozenModel):
    """A versioned target set (``config/targets.yaml``, TGT-001).

    It expands to one target per horizon and price reference; `params` are validated by the
    target kind (for example execution latency for forward returns). Horizons are trading time
    (ADR 0026): ``<n>d`` is n trading days, any other label (``15m``, ``4h``) is that much market
    time; a label may not mix days with other units (ADR 0032).
    """

    kind: str
    horizons: list[str] = Field(min_length=1)
    price_refs: list[PriceRef] = Field(min_length=1)
    params: dict[str, Any] = {}

    @field_validator("horizons")
    @classmethod
    def _positive_horizons(cls, value: list[str]) -> list[str]:
        return check_horizon_labels(value)


class TrialClusteringConfig(FrozenModel):
    """How the effective number of independent trials is estimated (EXP-004, ADR 0026)."""

    correlation_threshold: float = Field(gt=0, lt=1)
    #: Pairs of trials with fewer common trading days (daily-summed returns) are independent.
    min_common_days: int = Field(gt=1)


class ExperimentsConfig(FrozenModel):
    """Experiment registry settings (``experiments:`` in ``config/base.yaml``)."""

    trial_clustering: TrialClusteringConfig


class DatasetsConfig(FrozenModel):
    """Dataset builder settings (``datasets:`` in ``config/base.yaml``)."""

    #: Target builds report the labels with a fill later than this after its intended time.
    fill_delay_report_s: float = Field(gt=0)


class DiscoveryConfig(FrozenModel):
    """The discovery window EDA may read (EDA-001, ADR 0036).

    The first `fraction` of the non-vault span, from a dataset's start to ``vault.start``, ending at
    a trading-day start; or, once it is fixed from the real data's depth, everything before `end`.
    """

    fraction: float = Field(gt=0, lt=1)
    end: AwareDatetime | None = None

    @field_validator("end")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)


class EdaBootstrapConfig(FrozenModel):
    """Stationary-bootstrap intervals of descriptive statistics (EDA-002)."""

    n_boot: int = Field(ge=100)
    ci_level: float = Field(gt=0, lt=1)
    #: The mean block length covers at least this many trading days of bars.
    min_block_days: int = Field(ge=1)


class DistributionsConfig(FrozenModel):
    """Return distributions (EDA-002)."""

    hill_tail_fraction: float = Field(gt=0, lt=0.5)


class DependenceConfig(FrozenModel):
    """Autocorrelation analysis (EDA-003): one trading day of lags, at least `min_lags`."""

    min_lags: int = Field(ge=1)
    ci_level: float = Field(gt=0, lt=1)


class SeasonalityConfig(FrozenModel):
    """Seasonality and session effects (EDA-004).

    `event_windows` add windows around event anchors of ``config/sessions.yaml`` to the dataset's
    own event windows (which EDA always uses).
    """

    intraday_timeframe: Timeframe
    event_timeframe: Timeframe
    alpha: float = Field(gt=0, lt=1)
    event_windows: dict[str, EventWindow] = {}


class TrendConfig(FrozenModel):
    """Trend and reversion descriptives (EDA-005)."""

    variance_ratio_timeframe: Timeframe
    variance_ratio_q: list[int] = Field(min_length=1)
    run_timeframe: Timeframe

    @field_validator("variance_ratio_q")
    @classmethod
    def _check_q(cls, value: list[int]) -> list[int]:
        if any(q < 2 for q in value) or len(set(value)) != len(value):
            raise ValueError("variance-ratio horizons must be unique and at least 2 bars")
        return sorted(value)


class TargetSetRef(FrozenModel):
    """A target set of ``config/targets.yaml`` by name and version."""

    name: str
    version: str


class HorizonAdmissionConfig(FrozenModel):
    """Cost-to-volatility horizon admission (EDA-006, ADR 0037, ADR 0040).

    `candidates` are TGT-002 horizon labels (trading time; ``1d`` is one trading day). Holding
    periods start at the decisions of 1m bars on a `decision_step` grid and use the execution
    latency and allowed fill delay of the forward-return target set `target_set`.
    """

    max_cost_to_vol: float = Field(gt=0)
    candidates: list[str] = Field(min_length=1)
    decision_step: Timeframe
    target_set: TargetSetRef
    #: Slippage uses sigma-hat of 1-minute returns: their RMS over this many minutes before entry.
    sigma_1m_minutes: int = Field(ge=1)

    @field_validator("candidates")
    @classmethod
    def _labels(cls, value: list[str]) -> list[str]:
        return check_horizon_labels(value)


class EdaConfig(FrozenModel):
    """Exploratory research settings (``config/eda.yaml``, EDA-001 ... EDA-006)."""

    discovery: DiscoveryConfig
    timeframes: list[Timeframe] = Field(min_length=1)
    bootstrap: EdaBootstrapConfig
    distributions: DistributionsConfig
    dependence: DependenceConfig
    seasonality: SeasonalityConfig
    trend: TrendConfig
    horizons: HorizonAdmissionConfig


def _check_quantiles(value: list[float]) -> list[float]:
    if any(not 0 < q < 1 for q in value) or value != sorted(set(value)):
        raise ValueError("regime quantiles must be distinct, increasing and inside (0, 1)")
    return value


class StationarityConfig(FrozenModel):
    """The stationarity battery (STAT-001, ADR 0043)."""

    trend: Literal["n", "c", "ct"] = "c"
    adf_lag_method: Literal["aic", "bic", "t-stat"] = "aic"
    adf_max_lags: int | None = Field(default=None, ge=0)
    kpss_trends: list[Literal["c", "ct"]] = Field(min_length=1)
    zivot_andrews_trim: float = Field(gt=0, lt=0.5)

    @model_validator(mode="after")
    def _level_kpss(self) -> StationarityConfig:
        if "c" not in self.kpss_trends:
            raise ValueError("stats.stationarity.kpss_trends must include 'c' (the joint verdict)")
        return self


class StatsDependenceConfig(FrozenModel):
    """Ljung-Box and ARCH-LM lags (STAT-002)."""

    ljung_box_lags: list[int] = Field(min_length=1)
    arch_lm_lags: list[int] = Field(min_length=1)

    @field_validator("ljung_box_lags", "arch_lm_lags")
    @classmethod
    def _positive(cls, value: list[int]) -> list[int]:
        if any(lag < 1 for lag in value) or len(set(value)) != len(value):
            raise ValueError("lags must be unique and at least 1")
        return sorted(value)


class VarianceRatioConfig(FrozenModel):
    """Variance-ratio tests (STAT-003)."""

    horizons: list[int] = Field(min_length=1)
    regime_window: int = Field(ge=2)
    regime_quantiles: list[float] = Field(min_length=1)

    @field_validator("horizons")
    @classmethod
    def _check_horizons(cls, value: list[int]) -> list[int]:
        if any(q < 2 for q in value) or len(set(value)) != len(value):
            raise ValueError("variance-ratio horizons must be unique and at least 2 bars")
        return sorted(value)

    @field_validator("regime_quantiles")
    @classmethod
    def _check_quantiles(cls, value: list[float]) -> list[float]:
        return _check_quantiles(value)


class ArmaSpec(FrozenModel):
    """An ARMA(p, q) model of 1-bar log returns, or an AR(p) with p chosen by AIC (`max_p`)."""

    p: int = Field(default=0, ge=0)
    q: int = Field(default=0, ge=0)
    max_p: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _order(self) -> ArmaSpec:
        if self.max_p is not None and (self.p or self.q):
            raise ValueError("an AIC-selected AR model takes max_p only (no p, no q)")
        if self.max_p is None and self.p + self.q == 0:
            raise ValueError("an ARMA model needs p + q >= 1 (the zero forecast is a benchmark)")
        return self


class ArimaConfig(FrozenModel):
    """Walk-forward ARMA forecasts (STAT-006)."""

    models: dict[str, ArmaSpec] = Field(min_length=1)
    benchmarks: list[Literal["zero_return", "random_walk"]] = Field(min_length=1)


class StatsConfig(FrozenModel):
    """Statistical time-series research settings (``config/stats.yaml``, Phase 5)."""

    alpha: float = Field(gt=0, lt=1)
    stationarity: StationarityConfig
    dependence: StatsDependenceConfig
    variance_ratio: VarianceRatioConfig
    arima: ArimaConfig


class EstimatorsConfig(FrozenModel):
    """Range estimators (VOL-001)."""

    window: int = Field(ge=2)
    atr_window: int = Field(ge=1)


class DiurnalConfig(FrozenModel):
    """The intraday diurnal factor, fitted on training rows only (VOL-002)."""

    day_standardized: bool = True
    min_count: int = Field(ge=1)


class RealizedConfig(FrozenModel):
    """Realized measures (VOL-002)."""

    intraday_timeframes: list[Timeframe] = Field(min_length=1)
    periods: list[Timeframe] = Field(min_length=1)
    diurnal: DiurnalConfig

    @field_validator("periods")
    @classmethod
    def _check_periods(cls, value: list[Timeframe]) -> list[Timeframe]:
        allowed = {Timeframe("1h"), Timeframe("1d")}
        if any(tf not in allowed for tf in value):
            raise ValueError("realized periods are 1h (UTC hours) or 1d (trading days)")
        return value


class VolBenchmarksConfig(FrozenModel):
    """Volatility benchmarks with parameters fixed in advance (VOL-003)."""

    rolling_windows: list[int] = Field(min_length=1)
    ewma_lambdas: list[float] = Field(min_length=1)
    har_components: list[int] = Field(min_length=1)
    har_intraday_components: list[int] = Field(min_length=1)
    variance_floor_share: float = Field(gt=0, lt=1)

    @field_validator("ewma_lambdas")
    @classmethod
    def _check_lambdas(cls, value: list[float]) -> list[float]:
        if any(not 0 < lam < 1 for lam in value):
            raise ValueError("EWMA lambdas lie in (0, 1)")
        return value

    @field_validator("rolling_windows", "har_components", "har_intraday_components")
    @classmethod
    def _check_windows(cls, value: list[int]) -> list[int]:
        if any(w < 1 for w in value) or len(set(value)) != len(value):
            raise ValueError("windows must be unique and at least 1 period")
        return sorted(value)


class GarchSpec(FrozenModel):
    """A GARCH-family volatility process: GARCH or EGARCH, with (o = 1) or without asymmetry."""

    vol: Literal["GARCH", "EGARCH"]
    o: int = Field(default=0, ge=0, le=1)


class GarchConfig(FrozenModel):
    """GARCH-family models (VOL-004)."""

    mean: Literal["zero", "constant"] = "zero"
    models: dict[str, GarchSpec] = Field(min_length=1)
    distributions: list[Literal["normal", "t", "skewt"]] = Field(min_length=1)
    simulations: int = Field(ge=100)


class VolEvaluationConfig(FrozenModel):
    """Volatility forecast evaluation (VOL-005)."""

    target: Literal["rv"] = "rv"
    mcs_alpha: float = Field(gt=0, lt=1)
    mcs_n_boot: int = Field(ge=100)
    mcs_mean_block: float = Field(ge=1)
    dm_alpha: float = Field(gt=0, lt=1)
    dm_reference: str
    regime_window: int = Field(ge=1)
    regime_quantiles: list[float] = Field(min_length=1)

    @field_validator("regime_quantiles")
    @classmethod
    def _check_quantiles(cls, value: list[float]) -> list[float]:
        return _check_quantiles(value)


class VolSelectionConfig(FrozenModel):
    """Which forecaster serves sigma-hat when nothing beats it (VOL-006)."""

    default: str


class VolatilityConfig(FrozenModel):
    """Volatility research settings (``config/volatility.yaml``, Phase 6)."""

    estimators: EstimatorsConfig
    realized: RealizedConfig
    benchmarks: VolBenchmarksConfig
    garch: GarchConfig
    evaluation: VolEvaluationConfig
    selection: VolSelectionConfig

    def benchmark_names(self) -> list[str]:
        """Names of the VOL-003 benchmarks on the volatility board (``xq.research.volatility``)."""
        return [
            *(f"rolling_{w}" for w in self.benchmarks.rolling_windows),
            *(f"ewma_{lam:g}" for lam in self.benchmarks.ewma_lambdas),
            "har",
        ]

    @model_validator(mode="after")
    def _check_names(self) -> VolatilityConfig:
        names = self.benchmark_names()
        if self.selection.default not in names:
            raise ValueError(
                f"volatility.selection.default {self.selection.default!r} is not a benchmark "
                f"({names})"
            )
        if self.evaluation.dm_reference not in names:
            raise ValueError(
                f"volatility.evaluation.dm_reference {self.evaluation.dm_reference!r} is not a "
                f"benchmark ({names})"
            )
        return self


class SpaSizeCheckConfig(FrozenModel):
    """The per-sample size check of SPA and the Reality Check (VAL-004, C-24, ADR 0055)."""

    #: Simulated null families per check.
    n_sim: int = Field(ge=50)
    #: Bootstrap resamples per simulated family.
    n_boot: int = Field(ge=99)
    #: Highest AR order of the sieve fitted to each strategy's differentials (chosen by AIC).
    max_ar_order: int = Field(ge=0, le=20)
    #: A gate result that uses SPA or the Reality Check carries a warning when the simulated
    #: rejection rate exceeds this multiple of the gate's level.
    warn_ratio: float = Field(gt=1)


class PerturbationConfig(FrozenModel):
    """Parameter perturbation (ROB-001; the neighbourhood design of C-24, ADR 0055)."""

    #: Perturbation levels, shares of each parameter's scale; the gate reads its own
    #: ``parameter_neighbourhood.perturbation``, which must be one of them.
    levels: list[Annotated[float, Field(gt=0, lt=1)]] = Field(min_length=1)
    #: The largest joint neighbourhood evaluated in full (3^5 - 1 = 242 points for five
    #: parameters); a larger one is a seeded sample of this many points.
    max_joint_points: int = Field(ge=1)


class MonteCarloConfig(FrozenModel):
    """Monte Carlo equity with the risk rules applied (ROB-004)."""

    #: Resampled paths replayed through the risk engine.
    n_paths: int = Field(ge=100)
    #: One R: a stop at this many daily sigma-hats (the event tier's default stop).
    stop_sigmas: float = Field(gt=0)
    #: A path is ruined when its equity falls to this share of the capital.
    ruin_level: float = Field(gt=0, lt=1)
    #: Lower bound on the Politis-White mean block of the resampled trade outcomes (trades).
    min_block_trades: int = Field(ge=1)


class NoiseConfig(FrozenModel):
    """Noise injection (ROB-005): levels above zero and draws per level."""

    #: Price noise, in multiples of the spread.
    price_levels: list[Annotated[float, Field(gt=0)]] = Field(min_length=1)
    #: Feature noise, in multiples of each feature's causal standard deviation.
    feature_levels: list[Annotated[float, Field(gt=0)]] = Field(min_length=1)
    n_seeds: int = Field(ge=1)


class ValidationConfig(FrozenModel):
    """Validation and robustness procedures (``config/validation.yaml``, Phases 16 and 17).

    Pass/fail thresholds are not here: they are in ``config/gates.yaml``.
    """

    spa_size_check: SpaSizeCheckConfig
    perturbation: PerturbationConfig
    monte_carlo: MonteCarloConfig
    noise: NoiseConfig


class SpreadCostConfig(FrozenModel):
    """Spread fallback when quotes carry no bid/ask (BT-001)."""

    fallback_quantile: Literal["p50", "p90", "p99"] = "p90"


class CommissionConfig(FrozenModel):
    """Commission per fill (per side), per lot and/or per notional."""

    per_lot_per_side_usd: float = Field(default=0.0, ge=0)
    per_notional_per_side_bps: float = Field(default=0.0, ge=0)


class SlippageConfig(FrozenModel):
    """Slippage in basis points: ``(fixed_bps + sigma_multiple * sigma_1m_bps) * multiplier``.

    `multipliers` are keyed by a session, overlap or event-window name of ``config/sessions.yaml``
    (``rollover_window`` for ``in_rollover_window``); the largest one that applies at the fill time
    is used, 1 when none does.
    """

    fixed_bps: float = Field(ge=0)
    sigma_multiple: float = Field(ge=0)
    multipliers: dict[str, float] = {}

    @field_validator("multipliers")
    @classmethod
    def _at_least_one(cls, value: dict[str, float]) -> dict[str, float]:
        low = [k for k, v in value.items() if v < 1]
        if low:
            raise ValueError(f"slippage multipliers must be at least 1: {low}")
        return value


class FinancingConfig(FrozenModel):
    """Overnight financing (swap) charged at each daily rollover on the notional held over it.

    Rates are annual percentages; positive is a cost, negative a credit. The rollover of
    `triple_weekday` (a trading day in New York) is charged three times, covering the weekend.
    """

    long_rate_annual_pct: float
    short_rate_annual_pct: float
    day_count: Literal[360, 365] = 360
    triple_weekday: Weekday = "wed"


class CostModelConfig(FrozenModel):
    """A venue's cost model (``config/costs/<name>.yaml``, BT-001).

    A provisional model (placeholder values, no broker terms) must charge financing on both sides:
    long and short rates strictly positive (ADR 0032). Only broker terms may credit a side.
    """

    venue: str
    provisional: bool
    latency_ms: int = Field(ge=0)
    max_fill_delay_s: float = Field(gt=0)
    spread: SpreadCostConfig = SpreadCostConfig()
    commission: CommissionConfig = CommissionConfig()
    slippage: SlippageConfig
    financing: FinancingConfig

    @model_validator(mode="after")
    def _provisional_financing_is_a_cost(self) -> CostModelConfig:
        financing = self.financing
        if (
            self.provisional
            and min(financing.long_rate_annual_pct, financing.short_rate_annual_pct) <= 0
        ):
            raise ValueError(
                "a provisional cost model must charge financing on longs and shorts (both rates "
                "> 0) until broker terms replace it (ADR 0032)"
            )
        return self


class EntryBlackoutConfig(FrozenModel):
    """Entry blackouts of the event backtester (BT-008).

    No order that opens, increases or flips exposure fills inside a blackout: inside any of the
    `event_windows` (names of ``event_windows`` in ``config/sessions.yaml``) or within
    `before_weekly_close_min` minutes before a close followed by at least a day without trading.
    Orders that only reduce exposure are never blocked.
    """

    event_windows: list[str] = []
    before_weekly_close_min: int = Field(default=0, ge=0)


class EventBacktestConfig(FrozenModel):
    """Event-driven backtester settings (``backtest.event``, BT-005 ... BT-009, ADR 0049).

    `margin_rate` is margin per unit of notional (PROVISIONAL until the broker is named).
    `flat_before_weekend` closes every position `flat_before_weekend_min` minutes before the
    weekly close. `reconcile_tolerance` is the largest equity difference between the tiers on a
    shared market-order strategy, as a share of its total costs (BT-009).
    """

    margin_rate: float = Field(gt=0, le=1)
    blackouts: EntryBlackoutConfig = EntryBlackoutConfig()
    flat_before_weekend: bool = False
    flat_before_weekend_min: int = Field(default=30, ge=1)
    reconcile_tolerance: float = Field(default=0.05, gt=0)


class SizingConfig(FrozenModel):
    """Position sizing of the risk engine (RISK-002, ``config/risk/<profile>.yaml``).

    ``fixed_fractional`` risks `risk_per_trade` of equity to the stop; ``vol_target`` sizes the
    position to `vol_target_annual` of annualized volatility. Either is capped by the strategy's
    requested exposure and, for an intent with a calibrated win probability, scaled by its edge
    per unit of risk (ADR 0053): ``ev_r = p_lcb x TP/SL - (1 - p_lcb) - cost/SL`` with ``p_lcb``
    the probability's lower confidence bound at `lcb_z` standard errors, and the multiplier
    ``clip(ev_r / ev_r_full, 0, 1)``. The drawdown throttle scales it too (1 up to
    `throttle_start`, falling linearly to 0 at `throttle_end`); then it is rounded down to the lot
    step.
    """

    method: Literal["fixed_fractional", "vol_target"]
    risk_per_trade: float = Field(gt=0, le=0.05)
    vol_target_annual: float = Field(gt=0)
    ev_r_full: float = Field(gt=0)
    lcb_z: float = Field(ge=0)
    throttle_start: float = Field(ge=0, lt=1)
    throttle_end: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> SizingConfig:
        if self.throttle_start >= self.throttle_end:
            raise ValueError("throttle_start must be below throttle_end")
        return self


class RiskLimitsConfig(FrozenModel):
    """Limits and halts of the risk engine (RISK-003). Fractions are of equity.

    New exposure is refused while the worst drawdown is at least `max_drawdown` (until a manual
    reset), while the trading day's loss is at least `max_daily_loss` of its starting equity,
    for `cooldown_minutes` after `max_consecutive_losses` losing round trips in a row, and once
    `max_trades_per_day` entries have filled that day. Every position is capped at `max_lots`,
    `max_notional` and `max_margin_use`, and during a session named in `session_max_exposure` at
    that exposure.
    """

    max_lots: float = Field(gt=0)
    max_notional: float = Field(gt=0)
    max_margin_use: float = Field(gt=0, le=1)
    max_daily_loss: float = Field(gt=0, lt=1)
    max_drawdown: float = Field(gt=0, lt=1)
    max_consecutive_losses: int = Field(ge=1)
    cooldown_minutes: int = Field(ge=0)
    max_trades_per_day: int = Field(ge=1)
    session_max_exposure: dict[str, float] = {}


class StopPolicyConfig(FrozenModel):
    """Stop policy (RISK-004): every entry carries a stop at a distance in
    ``[min_spread_multiple x spread, max_sigma_multiple x daily sigma-hat x price]``; a closer stop
    is widened to the minimum, a farther one refused."""

    required: bool = True
    min_spread_multiple: float = Field(ge=0)
    max_sigma_multiple: float = Field(gt=0)


class KillSwitchConfig(FrozenModel):
    """Manual kill switch (RISK-006): new exposure is refused while `file` exists or `env_var`
    is set to a true value; with `flatten`, open positions are closed too."""

    file: Path | None = None
    env_var: str | None = None
    flatten: bool = False


class BreakersConfig(FrozenModel):
    """Data-health breakers (RISK-006): no new exposure on a quote older than `stale_quote_s`
    or with a spread above `spread_multiple` times the median of the last `spread_window` quotes."""

    stale_quote_s: float = Field(gt=0)
    spread_multiple: float = Field(gt=1)
    spread_window: int = Field(ge=10)


class RiskSigmaConfig(FrozenModel):
    """The event tier's interim daily sigma-hat when none is supplied: an EWMA of signal-bar log
    returns (span `span_bars`), scaled to one trading day, known after `min_bars` returns."""

    span_bars: int = Field(ge=2)
    min_bars: int = Field(ge=2)


class RiskConfig(FrozenModel):
    """A risk profile (``config/risk/<profile>.yaml``, Phase 14). `version` is recorded on every
    risk decision together with a hash of the whole profile."""

    version: str = Field(min_length=1)
    provisional: bool
    sizing: SizingConfig
    limits: RiskLimitsConfig
    stops: StopPolicyConfig
    kill_switch: KillSwitchConfig = KillSwitchConfig()
    breakers: BreakersConfig
    sigma: RiskSigmaConfig


class BacktestConfig(FrozenModel):
    """Backtest settings (``backtest:`` in ``config/base.yaml``)."""

    cost_model: str
    risk_profile: str | None = None
    capital_usd: float = Field(gt=0)
    periods_per_year: int = Field(gt=0)
    event: EventBacktestConfig | None = None

    def event_config(self) -> EventBacktestConfig:
        """The event-backtester settings; raise if they are not configured."""
        if self.event is None:
            raise ConfigError("no event-backtester configuration (backtest.event in base.yaml)")
        return self.event


# Evidence gates (VAL-007, ``config/gates.yaml``, ADR 0032). Thresholds are fixed before any
# candidate result exists; how each is compared at its boundary is fixed in `GatesConfig.criteria`.
Probability = Annotated[float, Field(gt=0, lt=1)]
Share = Annotated[float, Field(gt=0, le=1)]
SharpeFloor = Annotated[float, Field(ge=0)]
GateComparison = Literal[">", ">=", "<", "<="]
PERIODS_PER_YEAR_REF = "backtest.periods_per_year"


@dataclass(frozen=True)
class GateCriterion:
    """One pass/fail comparison of a gate: ``value <op> threshold`` passes; a missing value fails.

    `key` is the threshold's dotted path in ``config/gates.yaml`` (``.low`` / ``.high`` for the two
    sides of an interval); `measure` says what value is compared.
    """

    gate: str
    key: str
    measure: str
    op: GateComparison
    threshold: float

    def passes(self, value: float) -> bool:
        """Whether `value` satisfies the criterion (NaN never does)."""
        if math.isnan(value):
            return False
        if self.op == ">":
            return value > self.threshold
        if self.op == ">=":
            return value >= self.threshold
        if self.op == "<":
            return value < self.threshold
        return value <= self.threshold

    def check(self, value: float, warnings: tuple[str, ...] = ()) -> GateCheck:
        """The criterion applied to a measured `value`, with any `warnings` about the evidence."""
        return GateCheck(self, float(value), tuple(warnings))


@dataclass(frozen=True)
class GateCheck:
    """A gate criterion applied to a measured value (robustness and validation evidence).

    `warnings` qualify the evidence without changing the outcome: a threshold is never moved,
    but a reader must see, next to the result, when the test behind it is known to be unreliable
    on the sample (for example SPA over-rejecting under strong serial dependence, ADR 0055).
    """

    criterion: GateCriterion
    value: float
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Whether the value satisfies the criterion (NaN never does)."""
        return self.criterion.passes(self.value)

    def describe(self) -> str:
        """One line: the measure, its value, the rule and the outcome."""
        c = self.criterion
        outcome = "pass" if self.passed else "FAIL"
        line = (
            f"{c.gate} {c.key}: {c.measure} = {self.value:.4g} ({c.op} {c.threshold:g}) {outcome}"
        )
        return "".join([line, *(f"; WARNING: {w}" for w in self.warnings)])


class GateBootstrapConfig(FrozenModel):
    """Stationary bootstrap of daily net returns used by gate statistics (VAL-001)."""

    n_boot: int = Field(ge=1000)
    #: ``politis_white``: the automatic mean block length of Politis and White (2004), with the
    #: Patton, Politis and White (2009) correction; or a fixed mean block length in days.
    block_length: Literal["politis_white"] | Annotated[int, Field(ge=1)]
    #: The mean block length is never shorter than this many days.
    min_block_days: int = Field(ge=1)


class GateConventions(FrozenModel):
    """How the statistics a gate compares are computed."""

    returns: Literal["daily_net"]
    annualization: Literal["backtest.periods_per_year"] | Annotated[int, Field(gt=0)]
    trial_count: Literal["effective", "raw"]
    report_raw_trial_count: bool
    raw_vs_effective_review_ratio: float = Field(gt=1)
    one_sided: Literal[True]
    bootstrap: GateBootstrapConfig

    def needs_trial_review(self, n_raw: int, n_effective: float) -> bool:
        """True when raw trials exceed the effective count by more than the review ratio."""
        if n_raw <= 0 or n_effective <= 0:
            return False
        return n_raw / n_effective > self.raw_vs_effective_review_ratio


class R1Gate(FrozenModel):
    """R1 research candidate: moves a candidate to the event backtest and robustness work."""

    oos_net_sharpe_min: SharpeFloor
    oos_sharpe_p_max: Probability
    best_baseline_p_max: Probability
    best_baseline_margin_sharpe: SharpeFloor
    min_oos_trades: int = Field(ge=1)


class StressedCostsGate(FrozenModel):
    spread_multiplier: float = Field(ge=1)
    slippage_multiplier: float = Field(ge=1)
    net_sharpe_min: SharpeFloor


class NeighbourhoodGate(FrozenModel):
    perturbation: Probability
    profitable_share_min: Share


class MonteCarloDrawdownGate(FrozenModel):
    quantile: Probability
    below: Probability


class DecayTrendGate(FrozenModel):
    significance: Probability


class ExecutionDelayGate(FrozenModel):
    bars: int = Field(ge=1)
    net_sharpe_min: SharpeFloor


class MinTrackRecordGate(FrozenModel):
    confidence: Probability


class R2Gate(FrozenModel):
    """R2 validated: allows one vault evaluation."""

    dsr_min: Probability
    pbo_max: Probability
    spa_p_max: Probability
    stressed_costs: StressedCostsGate
    parameter_neighbourhood: NeighbourhoodGate
    positive_folds_share_min: Share
    max_single_year_pnl_share: Share
    monte_carlo_drawdown: MonteCarloDrawdownGate
    oos_max_drawdown_max: Probability
    decay_trend: DecayTrendGate
    execution_delay: ExecutionDelayGate
    min_track_record: MinTrackRecordGate


class R3Gate(FrozenModel):
    """R3 vault pass: moves a candidate to paper trading."""

    net_sharpe_min: SharpeFloor
    walk_forward_interval: Probability
    risk_limit_breaches_max: int = Field(ge=0)
    vault_access_logged: Literal[True]


class R4Gate(FrozenModel):
    """R4 paper pass: makes a candidate eligible for the live review."""

    min_months: int = Field(ge=1)
    min_trades: int = Field(ge=1)
    realized_slippage_ratio_max: float = Field(gt=0)
    monte_carlo_percentile_min: Probability
    shadow_parity_min: Share
    unresolved_incidents_max: int = Field(ge=0)


class GatesConfig(FrozenModel):
    """The evidence policy (``config/gates.yaml``, VAL-007, ADR 0032)."""

    version: Literal[1]
    conventions: GateConventions
    r1_research_candidate: R1Gate
    r2_validated: R2Gate
    r3_vault_pass: R3Gate
    r4_paper_pass: R4Gate

    def criteria(self) -> list[GateCriterion]:
        """Every pass/fail comparison of R1-R4 with its boundary rule (ADR 0032).

        Where the development plan states the comparison it is used as written ("> 0", "p < 0.05",
        "at least", "no single year above", "below the halt level", "p <= 0.10"); otherwise
        ``_min`` means at least and ``_max`` at most, except that a Sharpe floor must be exceeded
        (a Sharpe ratio of exactly 0 is no edge).
        """
        r1, r2 = self.r1_research_candidate, self.r2_validated
        r3, r4 = self.r3_vault_pass, self.r4_paper_pass
        stress, hood = r2.stressed_costs, r2.parameter_neighbourhood
        tail = round((1 - r3.walk_forward_interval) / 2, 12)
        c = GateCriterion
        return [
            c("R1", "oos_net_sharpe_min", "stitched OOS net Sharpe", ">", r1.oos_net_sharpe_min),
            c(
                "R1",
                "oos_sharpe_p_max",
                "one-sided stationary-bootstrap p-value of OOS net Sharpe > 0",
                "<",
                r1.oos_sharpe_p_max,
            ),
            c(
                "R1",
                "best_baseline_p_max",
                "one-sided paired block-bootstrap p-value of beating the best baseline",
                "<",
                r1.best_baseline_p_max,
            ),
            c(
                "R1",
                "best_baseline_margin_sharpe",
                "OOS net Sharpe minus the best baseline's",
                ">",
                r1.best_baseline_margin_sharpe,
            ),
            c("R1", "min_oos_trades", "closed OOS trades", ">=", r1.min_oos_trades),
            c("R2", "dsr_min", "deflated Sharpe ratio (gated trial count)", ">=", r2.dsr_min),
            c("R2", "pbo_max", "probability of backtest overfitting (CSCV)", "<=", r2.pbo_max),
            c("R2", "spa_p_max", "Hansen SPA p-value for the family", "<=", r2.spa_p_max),
            c(
                "R2",
                "stressed_costs.net_sharpe_min",
                f"net Sharpe at {stress.spread_multiplier:g}x spread and "
                f"{stress.slippage_multiplier:g}x slippage",
                ">",
                stress.net_sharpe_min,
            ),
            c(
                "R2",
                "parameter_neighbourhood.profitable_share_min",
                f"share of the +/-{hood.perturbation:.0%} parameter neighbourhood with positive "
                "net P&L",
                ">=",
                hood.profitable_share_min,
            ),
            c(
                "R2",
                "positive_folds_share_min",
                "share of test folds with positive net P&L",
                ">=",
                r2.positive_folds_share_min,
            ),
            c(
                "R2",
                "max_single_year_pnl_share",
                "largest share of total net P&L earned in one calendar year",
                "<=",
                r2.max_single_year_pnl_share,
            ),
            c(
                "R2",
                "monte_carlo_drawdown.below",
                f"{r2.monte_carlo_drawdown.quantile:.0%} quantile of the Monte Carlo maximum "
                "drawdown",
                "<",
                r2.monte_carlo_drawdown.below,
            ),
            c("R2", "oos_max_drawdown_max", "OOS maximum drawdown", "<=", r2.oos_max_drawdown_max),
            c(
                "R2",
                "decay_trend.significance",
                "one-sided p-value of a negative slope of performance over time",
                ">=",
                r2.decay_trend.significance,
            ),
            c(
                "R2",
                "execution_delay.net_sharpe_min",
                f"net Sharpe with execution delayed by {r2.execution_delay.bars} bar(s)",
                ">",
                r2.execution_delay.net_sharpe_min,
            ),
            c(
                "R2",
                "min_track_record.confidence",
                f"OOS days over the minimum track record length at "
                f"{r2.min_track_record.confidence:.0%} confidence",
                ">=",
                1.0,
            ),
            c("R3", "net_sharpe_min", "vault net Sharpe", ">", r3.net_sharpe_min),
            c(
                "R3",
                "walk_forward_interval.low",
                "quantile of the vault net Sharpe in the walk-forward bootstrap distribution",
                ">=",
                tail,
            ),
            c(
                "R3",
                "walk_forward_interval.high",
                "quantile of the vault net Sharpe in the walk-forward bootstrap distribution",
                "<=",
                1 - tail,
            ),
            c(
                "R3",
                "risk_limit_breaches_max",
                "risk-limit breaches in the vault run",
                "<=",
                r3.risk_limit_breaches_max,
            ),
            c("R3", "vault_access_logged", "vault access logged (1 yes, 0 no)", ">=", 1.0),
            c("R4", "min_months", "months of paper trading", ">=", r4.min_months),
            c("R4", "min_trades", "paper trades", ">=", r4.min_trades),
            c(
                "R4",
                "realized_slippage_ratio_max",
                "realized over modelled slippage",
                "<=",
                r4.realized_slippage_ratio_max,
            ),
            c(
                "R4",
                "monte_carlo_percentile_min",
                "percentile of paper performance in the Monte Carlo band",
                ">",
                r4.monte_carlo_percentile_min,
            ),
            c(
                "R4",
                "shadow_parity_min",
                "share of decisions identical to shadow replay",
                ">=",
                r4.shadow_parity_min,
            ),
            c(
                "R4",
                "unresolved_incidents_max",
                "unresolved incidents",
                "<=",
                r4.unresolved_incidents_max,
            ),
        ]

    def criterion(self, gate: str, key: str) -> GateCriterion:
        """The criterion of `gate` (``"R2"``) with threshold `key` (a dotted gates.yaml path).

        Raises:
            KeyError: for an unknown gate or key.
        """
        for item in self.criteria():
            if (item.gate, item.key) == (gate, key):
                return item
        raise KeyError(f"no criterion {key!r} in gate {gate!r}")


def gates_hash(gates: GatesConfig) -> str:
    """16-hex SHA-256 of the evidence policy's canonical JSON (recorded with gate evidence)."""
    canonical = json.dumps(gates.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class DownloadConfig(FrozenModel):
    """Endpoint and politeness of a vendor downloader (`xq fetch dukascopy`, ADR 0057).

    One request at a time, at least `min_interval_s` apart. A failed request (network error,
    timeout, HTTP 429 or 5xx) is retried after `backoff_s`, doubling each time, up to
    `max_attempts` tries; then the run stops (it resumes where it stopped). An empty answer for an
    hour inside market hours is asked again once after `empty_retry_pause_s` (0 disables the
    second request), and `max_empty_open_hours` consecutive empty hours inside market hours stop
    the run: the endpoint is more likely failing than the market silent for that long.
    `history_start` is the vendor's first day with ticks; earlier days are refused.
    """

    base_url: str = Field(pattern=r"^https?://")
    history_start: date
    min_interval_s: float = Field(gt=0)
    timeout_s: float = Field(gt=0)
    max_attempts: int = Field(ge=1)
    backoff_s: float = Field(ge=0)
    empty_retry_pause_s: float = Field(ge=0)
    max_empty_open_hours: int = Field(ge=1)


class SourceConfig(FrozenModel):
    """A declared market-data source (DATA-003). The clock convention is part of its identity.

    Vendor encodings (DATA-013, ADR 0057): `vendor_symbol` is the vendor's code for the instrument
    (Dukascopy ``XAUUSD``) and `point_scale` the number of integer price points per unit of the
    quote currency in the vendor's binary files (Dukascopy XAUUSD: 1000, so 2034155 is 2034.155).
    Both are required by the ``dukascopy_ticks`` adapter. `download` configures the source's
    downloader, where one exists.
    """

    adapter: Literal["mt5_ticks", "dukascopy_ticks"]
    vendor: str
    feed_type: Literal["broker_ticks", "vendor_ticks", "vendor_bars"]
    venue: str
    price_type: Literal["bid_ask_ticks", "bars"]
    clock: str
    instrument: str
    file_patterns: list[str] = ["*.csv"]
    encoding: str | None = None
    vendor_symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9]+$")
    point_scale: int | None = Field(default=None, gt=0)
    download: DownloadConfig | None = None
    notes: str = ""

    @field_validator("clock")
    @classmethod
    def _canonical_clock(cls, value: str) -> str:
        try:
            return str(ClockConvention.parse(value))
        except ClockConventionError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _check_vendor_encoding(self) -> SourceConfig:
        if self.adapter == "dukascopy_ticks":
            missing = [n for n in ("vendor_symbol", "point_scale") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"the dukascopy_ticks adapter needs {', '.join(missing)}")
        return self

    def clock_convention(self) -> ClockConvention:
        """The parsed clock convention."""
        return ClockConvention.parse(self.clock)


class DataConfig(FrozenModel):
    """Which declared source the data pipeline reads when a command names none (ADR 0057)."""

    primary_source: str


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
    data: DataConfig | None = None
    cleaning: CleaningConfig | None = None
    bars: BarsConfig | None = None
    quality: QualityConfig | None = None
    experiments: ExperimentsConfig | None = None
    datasets: DatasetsConfig | None = None
    costs: dict[str, CostModelConfig] = {}
    risk: dict[str, RiskConfig] = {}
    backtest: BacktestConfig | None = None
    targets: dict[str, dict[str, TargetSetConfig]] = {}
    gates: GatesConfig | None = None
    eda: EdaConfig | None = None
    stats: StatsConfig | None = None
    volatility: VolatilityConfig | None = None
    validation: ValidationConfig | None = None
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
        if self.data is not None and self.data.primary_source not in self.sources:
            raise ValueError(f"data.primary_source {self.data.primary_source!r} is not a source")
        return self

    def primary_source(self) -> str:
        """The source pipeline commands use by default (``data.primary_source``)."""
        if self.data is None:
            raise ConfigError("no primary source (data.primary_source in config/base.yaml)")
        return self.data.primary_source

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

    def quality_config(self) -> QualityConfig:
        """Return the data-quality thresholds; raise if they are not configured."""
        if self.quality is None:
            raise ConfigError("no quality configuration (config/quality.yaml) was loaded")
        return self.quality

    def target_set(self, name: str, version: str) -> TargetSetConfig:
        """Return the definition of target set `name` / `version`; raise if unknown."""
        try:
            return self.targets[name][version]
        except KeyError:
            known = ", ".join(f"{n}.{v}" for n, vs in sorted(self.targets.items()) for v in vs)
            raise ConfigError(
                f"unknown target set {name}.{version}; configured: {known or 'none'}"
            ) from None

    def experiments_config(self) -> ExperimentsConfig:
        """Return the experiment registry settings; raise if they are not configured."""
        if self.experiments is None:
            raise ConfigError("no experiments configuration (experiments: in config/base.yaml)")
        return self.experiments

    @model_validator(mode="after")
    def _check_cost_model(self) -> AppConfig:
        if self.backtest is not None and self.backtest.cost_model not in self.costs:
            known = ", ".join(sorted(self.costs)) or "none"
            raise ValueError(
                f"backtest.cost_model {self.backtest.cost_model!r} is not in config/costs "
                f"(available: {known})"
            )
        profile = self.backtest.risk_profile if self.backtest is not None else None
        if profile is not None and profile not in self.risk:
            known = ", ".join(sorted(self.risk)) or "none"
            raise ValueError(
                f"backtest.risk_profile {profile!r} is not in config/risk (available: {known})"
            )
        event = self.backtest.event if self.backtest is not None else None
        if event is not None and self.sessions is not None:
            unknown = sorted(set(event.blackouts.event_windows) - set(self.sessions.event_windows))
            if unknown:
                raise ValueError(
                    f"backtest.event.blackouts.event_windows {unknown} are not event windows of "
                    f"config/sessions.yaml ({', '.join(sorted(self.sessions.event_windows))})"
                )
        return self

    def backtest_config(self) -> BacktestConfig:
        """Return the backtest settings; raise if they are not configured."""
        if self.backtest is None:
            raise ConfigError("no backtest configuration (backtest: in config/base.yaml)")
        return self.backtest

    def risk_config(self, name: str | None = None) -> RiskConfig:
        """The risk profile `name` (default: ``backtest.risk_profile``)."""
        chosen = name if name is not None else self.backtest_config().risk_profile
        if chosen is None:
            raise ConfigError("no risk profile (backtest.risk_profile in config/base.yaml)")
        try:
            return self.risk[chosen]
        except KeyError:
            known = ", ".join(sorted(self.risk)) or "none"
            raise ConfigError(f"unknown risk profile {chosen!r}; configured: {known}") from None

    def cost_model_config(self, name: str | None = None) -> CostModelConfig:
        """The cost model `name` (default: ``backtest.cost_model``)."""
        chosen = name if name is not None else self.backtest_config().cost_model
        try:
            return self.costs[chosen]
        except KeyError:
            known = ", ".join(sorted(self.costs)) or "none"
            raise ConfigError(f"unknown cost model {chosen!r}; configured: {known}") from None

    @model_validator(mode="after")
    def _check_gate_conventions(self) -> AppConfig:
        if self.gates is None:
            return self
        conventions = self.gates.conventions
        if conventions.annualization == PERIODS_PER_YEAR_REF and self.backtest is None:
            raise ValueError(f"gates annualize with {PERIODS_PER_YEAR_REF}, which is not set")
        if conventions.trial_count == "effective" and self.experiments is None:
            raise ValueError(
                "gates count effective trials, but experiments.trial_clustering is not configured"
            )
        return self

    @model_validator(mode="after")
    def _check_validation(self) -> AppConfig:
        if self.gates is None or self.validation is None:
            return self
        gate_level = self.gates.r2_validated.parameter_neighbourhood.perturbation
        if not any(math.isclose(gate_level, x) for x in self.validation.perturbation.levels):
            raise ValueError(
                f"validation.perturbation.levels must include the gate's perturbation {gate_level}"
            )
        return self

    def gates_config(self) -> GatesConfig:
        """Return the evidence policy; raise if ``config/gates.yaml`` was not loaded."""
        if self.gates is None:
            raise ConfigError("no evidence gates (config/gates.yaml) were loaded")
        return self.gates

    def gate_periods_per_year(self) -> int:
        """Periods per year the gates annualize daily statistics with."""
        annualization = self.gates_config().conventions.annualization
        if annualization == PERIODS_PER_YEAR_REF:
            return self.backtest_config().periods_per_year
        return int(annualization)

    @model_validator(mode="after")
    def _check_eda(self) -> AppConfig:
        if self.eda is None:
            return self
        end = self.eda.discovery.end
        if end is not None and end > self.vault.start:
            raise ValueError(f"eda.discovery.end {end} is after vault.start {self.vault.start}")
        reference = self.eda.horizons.target_set
        definition = self.targets.get(reference.name, {}).get(reference.version)
        if definition is None or definition.kind != "forward_return":
            raise ValueError(
                f"eda.horizons.target_set {reference.name}.{reference.version} must be a "
                "configured forward_return target set"
            )
        windows = self.eda.seasonality.event_windows
        if windows:
            anchors = set(self.sessions.event_anchors) if self.sessions is not None else set()
            unknown = sorted(set(windows) - anchors)
            if unknown:
                raise ValueError(f"eda.seasonality.event_windows for unknown anchors: {unknown}")
        return self

    def eda_config(self) -> EdaConfig:
        """Return the exploratory research settings; raise if ``config/eda.yaml`` was not loaded."""
        if self.eda is None:
            raise ConfigError("no exploratory research configuration (config/eda.yaml) was loaded")
        return self.eda

    def stats_config(self) -> StatsConfig:
        """Return the statistical research settings; raise if ``config/stats.yaml`` is missing."""
        if self.stats is None:
            raise ConfigError("no statistical research configuration (config/stats.yaml)")
        return self.stats

    def volatility_config(self) -> VolatilityConfig:
        """Return the volatility research settings (``config/volatility.yaml``), or raise."""
        if self.volatility is None:
            raise ConfigError("no volatility research configuration (config/volatility.yaml)")
        return self.volatility

    def validation_config(self) -> ValidationConfig:
        """Return the validation and robustness settings (``config/validation.yaml``), or raise."""
        if self.validation is None:
            raise ConfigError("no validation configuration (config/validation.yaml)")
        return self.validation

    def datasets_config(self) -> DatasetsConfig:
        """Return the dataset builder settings; raise if they are not configured."""
        if self.datasets is None:
            raise ConfigError("no datasets configuration (datasets: in config/base.yaml)")
        return self.datasets

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
    base_file = _read_yaml(directory / BASE_FILE)
    profile_layer = _read_profile(directory, profile)
    base_layer = deep_merge(_read_fragments(directory), base_file)
    file_layers = [base_layer, profile_layer]
    for path, layer in zip((BASE_FILE, f"{profile}.yaml"), file_layers, strict=True):
        _reject_secrets(layer, where=f"config file {path}")

    override_layer = nest_dotted(overrides or {})
    _reject_secrets(override_layer, where="overrides")

    env_layer = dict(EnvSettingsSource(AppConfig)())
    env_layer.pop("profile", None)  # the profile is chosen by the caller, not the environment
    for layer, where in (
        (base_file, f"config file {BASE_FILE}"),
        (profile_layer, f"config file {profile}.yaml"),
        (env_layer, f"{ENV_PREFIX}* environment variables"),
        (override_layer, "overrides"),
    ):
        _reject_file_only_sections(layer, where=where)

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


def _reject_file_only_sections(layer: Mapping[str, Any], *, where: str) -> None:
    for section, filename in FILE_ONLY_SECTIONS.items():
        if section in layer:
            raise ConfigError(
                f"{section!r} may only be set in config/{filename}, not in {where}: evidence "
                "thresholds are fixed before results are seen (ADR 0032)"
            )


def _reject_secrets(layer: Mapping[str, Any], *, where: str) -> None:
    section = layer.get(SECRETS_SECTION)
    if isinstance(section, Mapping) and any(v is not None for v in section.values()):
        raise ConfigError(
            f"secrets may only be set through {ENV_PREFIX}{SECRETS_SECTION.upper()}"
            f"{ENV_NESTED_DELIMITER}* environment variables, not in {where}"
        )
