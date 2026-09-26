"""Declarative dataset specifications and dataset identity (DS-001).

A `DatasetSpec` states everything that determines a dataset's content: the source and instrument,
the base timeframe and price basis, the window, the context timeframes, the feature and target
sets (by name and version), explicitly excluded partitions, the vault policy, and — once resolved
by the builder — the bar build version and the quality run the data was gated on.

``dataset_id = "ds-" + sha256(canonical spec JSON + dataset code version)[:16]``. Canonical JSON
has sorted keys and no whitespace, so the id does not depend on YAML key order or formatting. An
id is only defined for a *resolved* spec (bar build and quality run set): an unresolved spec
could produce different data depending on what is in the stores when it is built.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import pandas as pd
import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from xq.core.errors import ConfigError
from xq.core.types import PriceBasis, Timeframe

#: Bump when the dataset builder's logic changes; it is part of every dataset id.
DATASET_CODE_VERSION = 1
DATASET_ID_PREFIX = "ds-"

_NAME = r"^[a-z][a-z0-9_]*$"
_SET_VERSION = r"^v[1-9][0-9]*$"


def _parse_duration(value: object) -> object:
    # Accept pandas-style strings ("10D", "36h") as well as ISO 8601 and plain timedeltas.
    if isinstance(value, str):
        try:
            return pd.Timedelta(value).to_pytimedelta()
        except ValueError:
            return value
    return value


Duration = Annotated[timedelta, BeforeValidator(_parse_duration)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SetRef(_Frozen):
    """A feature or target set by name and version, e.g. ``base`` / ``v1``."""

    name: str = Field(pattern=_NAME)
    version: str = Field(pattern=_SET_VERSION)

    def __str__(self) -> str:
        return f"{self.name}.{self.version}"


class PartitionExclusion(_Frozen):
    """A trading day deliberately left out of a dataset, with the reason (recorded in manifests)."""

    trading_day: date
    reason: str = Field(min_length=1)


class DatasetSpec(_Frozen):
    """What a dataset contains. Every field is part of the dataset id.

    Attributes:
        name: Short label, e.g. ``ds_base``.
        source, instrument: Configured source id and its instrument.
        base_timeframe: One row per complete bar of this timeframe; decision time is the bar's
            ``available_at``.
        price_basis: Quote the price features are computed on (targets choose their own side).
        start, end: UTC window ``[start, end)`` of base-bar starts. `end` may not exceed
            ``vault.start`` (checked by the builder, which knows the configuration).
        warmup: History loaded before `start` so trailing features are defined at `start`; its
            rows are not part of the dataset.
        context_timeframes: Higher timeframes joined on availability (never on bar start).
        feature_set, target_set: Versioned feature and target definitions (no target set means
            a features-only dataset).
        external_series: Reserved for external data with its own ``available_at``; must be empty
            until an external-data task exists.
        exclusions: Trading days deliberately excluded (e.g. quality FAIL days, DQ-007).
        vault_policy: Only ``exclude``: research datasets never contain vault data.
        bar_build: Bar build version the data comes from (set by the builder when resolving).
        quality_run_id: Quality run the partitions were gated on (set by the builder).
    """

    name: str = Field(pattern=_NAME)
    source: str
    instrument: str
    base_timeframe: Timeframe
    price_basis: PriceBasis = PriceBasis.MID
    start: AwareDatetime
    end: AwareDatetime
    warmup: Duration = timedelta(0)
    context_timeframes: list[Timeframe] = []
    feature_set: SetRef
    target_set: SetRef | None = None
    external_series: list[str] = []
    exclusions: list[PartitionExclusion] = []
    vault_policy: Literal["exclude"] = "exclude"
    bar_build: str | None = None
    quality_run_id: str | None = None

    @field_validator("start", "end")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("warmup")
    @classmethod
    def _non_negative(cls, value: timedelta) -> timedelta:
        if value < timedelta(0):
            raise ValueError("warmup must not be negative")
        return value

    @field_validator("context_timeframes")
    @classmethod
    def _canonical_context(cls, value: list[Timeframe]) -> list[Timeframe]:
        if len(set(value)) != len(value):
            raise ValueError("context timeframes must be unique")
        return sorted(value, key=lambda tf: tf.nanos)

    @field_validator("external_series")
    @classmethod
    def _no_external_yet(cls, value: list[str]) -> list[str]:
        if value:
            raise ValueError(
                "external series are not supported yet: they need their own available_at "
                "(Phase 3 plan) and an ingestion task"
            )
        return value

    @field_validator("exclusions")
    @classmethod
    def _canonical_exclusions(cls, value: list[PartitionExclusion]) -> list[PartitionExclusion]:
        days = [item.trading_day for item in value]
        if len(set(days)) != len(days):
            raise ValueError("each trading day may be excluded only once")
        return sorted(value, key=lambda item: item.trading_day)

    @model_validator(mode="after")
    def _check_window(self) -> DatasetSpec:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        base = self.base_timeframe.nanos
        too_fine = [tf.value for tf in self.context_timeframes if tf.nanos <= base]
        if too_fine:
            raise ValueError(
                f"context timeframes must be longer than the base timeframe "
                f"{self.base_timeframe.value}: {too_fine}"
            )
        return self

    @property
    def is_resolved(self) -> bool:
        """True once the bar build and quality run are pinned."""
        return self.bar_build is not None and self.quality_run_id is not None

    @property
    def excluded_days(self) -> set[date]:
        """Trading days listed in `exclusions`."""
        return {item.trading_day for item in self.exclusions}

    def resolved(self, *, bar_build: str, quality_run_id: str) -> DatasetSpec:
        """Return a copy with the bar build and quality run pinned.

        Raises:
            ValueError: if the spec already pins different values.
        """
        for field_name, value in (("bar_build", bar_build), ("quality_run_id", quality_run_id)):
            current = getattr(self, field_name)
            if current is not None and current != value:
                raise ValueError(f"spec pins {field_name}={current!r}, not {value!r}")
        return self.model_copy(update={"bar_build": bar_build, "quality_run_id": quality_run_id})

    def canonical_json(self) -> str:
        """Sorted-key, whitespace-free JSON of the spec (the input to the dataset id)."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


def dataset_id(spec: DatasetSpec) -> str:
    """``ds-<16 hex>`` over the canonical spec and `DATASET_CODE_VERSION`.

    Raises:
        ValueError: if the spec is not resolved (see `DatasetSpec.resolved`).
    """
    if not spec.is_resolved:
        raise ValueError(
            "a dataset id needs a resolved spec (bar_build and quality_run_id set); "
            "the builder resolves specs before identifying them"
        )
    payload = json.dumps(
        {"spec": json.loads(spec.canonical_json()), "code": code_versions()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return DATASET_ID_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def code_versions() -> dict[str, int]:
    """Code versions of every builder whose output a dataset contains."""
    return {"dataset": DATASET_CODE_VERSION}


def load_spec(path: Path) -> DatasetSpec:
    """Read and validate a dataset spec from YAML.

    Raises:
        ConfigError: if the file is missing, is not YAML, or does not validate.
    """
    if not path.is_file():
        raise ConfigError(f"dataset spec not found: {path}")
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        return DatasetSpec.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid dataset spec {path}:\n{exc}") from exc


def dump_spec(spec: DatasetSpec) -> str:
    """The spec as YAML in field order (what the builder writes to ``spec.yaml``)."""
    return yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
