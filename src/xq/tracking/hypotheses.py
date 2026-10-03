"""Hypothesis pre-registration (EXP-002).

A hypothesis is written before it is tested, as ``experiments/hypotheses/H-XXXX.yaml`` (template:
``experiments/hypotheses/TEMPLATE.yaml``), and registered with ``xq exp register <path>``.
Registration validates the document and locks its exact text by SHA-256. Registering an edited
file creates the next version and marks the previous one superseded; experiments record the
version they test, so a hypothesis cannot be quietly rewritten after results exist.

Validation enforces the research standards the document must meet: a falsification criterion, a
trial budget, a discovery window that ends before the evaluation window starts (ideas found by
looking at data are tested on later data), and an evaluation window that ends at or before
``vault.start`` (the vault is only for the release gate).

The trial budget is at least one, except in the ``descriptive`` family: a descriptive hypothesis
(the standing hypothesis H-0000 that EDA runs belong to, ADR 0041) evaluates no trading
configuration, so its budget may be zero (ADR 0042).

Declared ``slices`` must come from the slice vocabulary (`xq.tracking.slices`): an unknown name
is refused here, at registration, before the text is locked (C-24, ADR 0055).

A strategy whose parameters were fixed before any data was seen (a published rule, a market
convention) may say so: ``parameters_fixed_a_priori: true`` with a ``source`` naming where they
come from. The declaration is locked with the text, and it is the only way the R2 neighbourhood
gate becomes not applicable (C-25, ADR 0058); without it, every numeric constant of a
parameter-free strategy is perturbed. A flag without a source, or a source without the flag, is
refused.

The family ids the platform records forecasting-model trials under (``linear_forecasts``,
``volatility_models``; ``xq.tracking.registry.RESERVED_FAMILIES``) are reserved: a hypothesis
registered in one would mix its trials with model evaluations (ADR 0046, ADR 0047).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import Engine

from xq.core.config import AppConfig
from xq.core.errors import ConfigError
from xq.core.types import Timeframe
from xq.tracking.registry import (
    HypothesisRef,
    RegistryError,
    add_hypothesis_version,
    check_family_not_reserved,
    get_hypothesis,
    hypothesis_text,
    text_hash,
)
from xq.tracking.slices import canonical_slice

HYPOTHESIS_ID = re.compile(r"^H-\d{4}$")
#: The only family whose trial budget may be zero (ADR 0042).
DESCRIPTIVE_FAMILY = "descriptive"


class Window(BaseModel):
    """A UTC time window ``[start, end)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class HypothesisDoc(BaseModel):
    """A pre-registered hypothesis (the fields of the plan's EXP-002)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=HYPOTHESIS_ID.pattern)
    title: str = Field(min_length=1)
    family: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    target: str = Field(min_length=1)
    information_set: list[str] = Field(min_length=1)
    horizon: str = Field(min_length=1)
    timeframe: Timeframe
    discovery_window: Window
    evaluation_window: Window
    primary_metric: str = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    falsification_criteria: list[str] = Field(min_length=1)
    trial_budget: int = Field(ge=0)
    planned_tests: list[str] = Field(min_length=1)
    slices: list[str] = []
    #: The strategy's parameters were fixed before any data was seen (module docstring).
    parameters_fixed_a_priori: bool = False
    #: Where parameters fixed a priori come from; required with the flag, refused without it.
    source: str | None = None

    @field_validator("family")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        try:
            check_family_not_reserved(value)
        except RegistryError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("slices")
    @classmethod
    def _known_slices(cls, value: list[str]) -> list[str]:
        for name in value:
            canonical_slice(name)  # raises SliceError (a ValueError) for an unknown name
        return value

    @model_validator(mode="after")
    def _a_priori_source(self) -> HypothesisDoc:
        has_source = self.source is not None and bool(self.source.strip())
        if self.parameters_fixed_a_priori and not has_source:
            raise ValueError("parameters_fixed_a_priori: true needs a source")
        if self.source is not None and not self.parameters_fixed_a_priori:
            raise ValueError(
                "source names where parameters fixed a priori come from; set "
                "parameters_fixed_a_priori: true or remove it"
            )
        return self

    @model_validator(mode="after")
    def _budget(self) -> HypothesisDoc:
        if self.trial_budget == 0 and self.family != DESCRIPTIVE_FAMILY:
            raise ValueError(
                f"trial_budget must be greater than 0 unless the family is "
                f"{DESCRIPTIVE_FAMILY!r} (family {self.family!r})"
            )
        return self

    def check_windows(self, vault_start: datetime) -> None:
        """Raise ConfigError unless discovery precedes evaluation, which must avoid the vault."""
        for name in ("discovery_window", "evaluation_window"):
            window: Window = getattr(self, name)
            if window.end <= window.start:
                raise ConfigError(f"{self.id}: {name} must end after it starts")
        if self.discovery_window.end > self.evaluation_window.start:
            raise ConfigError(
                f"{self.id}: the discovery window must end before the evaluation window starts"
            )
        if self.evaluation_window.end > vault_start:
            raise ConfigError(
                f"{self.id}: the evaluation window ends after vault.start ({vault_start}); the "
                "vault is only opened by the release gate"
            )


def load_hypothesis(path: Path, cfg: AppConfig) -> tuple[HypothesisDoc, str]:
    """Read and validate a hypothesis file; return the document and its exact text.

    Raises:
        ConfigError: if the file is missing, malformed, invalid, or named differently from its id.
    """
    if not path.is_file():
        raise ConfigError(f"hypothesis file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        doc = HypothesisDoc.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid hypothesis {path}:\n{exc}") from exc
    if path.stem != doc.id:
        raise ConfigError(f"{path.name} declares id {doc.id}; the file must be named {doc.id}.yaml")
    doc.check_windows(cfg.vault.start)
    return doc, text


def register_hypothesis(cfg: AppConfig, engine: Engine, path: Path) -> HypothesisRef:
    """Validate and register a hypothesis file; an edited file becomes a new version."""
    doc, text = load_hypothesis(path, cfg)
    return add_hypothesis_version(
        engine, doc.id, title=doc.title, family_id=doc.family, yaml_text=text
    )


def is_registered(engine: Engine, path: Path) -> bool:
    """True if the file's exact text is the latest registered version of its hypothesis."""
    text = path.read_text(encoding="utf-8")
    try:
        latest = get_hypothesis(engine, path.stem)
    except RegistryError:
        return False
    return latest.yaml_hash == text_hash(text)


def fixed_parameters_source(engine: Engine, hypothesis_id: str, version: int) -> str | None:
    """The source of parameters fixed a priori that a registered version declares, or None.

    Read from the locked text, as registered (module docstring).

    Raises:
        RegistryError: if the version is not registered.
        ConfigError: if the locked text declares the flag without a source.
    """
    data: Any = yaml.safe_load(hypothesis_text(engine, hypothesis_id, version))
    if not isinstance(data, dict) or not data.get("parameters_fixed_a_priori"):
        return None
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ConfigError(f"{hypothesis_id} v{version} fixes parameters a priori without a source")
    return source.strip()
