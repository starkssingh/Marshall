"""The feature registry and versioned feature sets (FEAT-001, FEAT-008).

`FEATURES` is a static, immutable mapping of every registered feature, collected explicitly from
the family modules. A **feature set** is named and versioned in ``config/features.yaml``: a list
of registered features with their parameters, each on the dataset's base bars or on the bars of
one of its context timeframes (FEAT-008), optionally with the base columns of ``base.v1``.

`compute_feature_set(specs, inputs, context)` computes it from the dataset builder's input frames
(``base`` and one frame per context timeframe, each with ``available_at_utc``):

- a base-timeframe feature is computed on the base bars and indexed by their availability, which
  is the decision time;
- a context-timeframe feature is computed on that timeframe's bars and joined onto the decision
  times with `asof_join` on ``available_at`` (never on the bar start), as ``mtf_<tf>_<column>``,
  with one provenance column ``mtf_<tf>_available_at`` per timeframe, which the leakage harness
  audits.

`resolve_feature_set(cfg, ref)` gives the builder a `FeatureSetDef` for a configured set as for
the built-in ``base.v1``. A configured set's definition (its instances and every feature's code
version) is hashed into the dataset's config digest and locked in ``feature_sets`` on first use
(`lock_feature_set`): changing it needs a new version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from xq.core.config import AppConfig, FeatureSetConfig
from xq.core.errors import ConfigError
from xq.core.time import utc_now
from xq.core.types import Timeframe
from xq.datasets.asof import PROVENANCE_SUFFIX
from xq.datasets.base_features import (
    BASE_INPUT,
    FEATURE_SETS,
    FeatureContext,
    FeatureSetDef,
    base_v1,
    decision_index,
)
from xq.datasets.leakage import Inputs
from xq.datasets.spec import SetRef
from xq.features.base import GATES, BarContext, Feature, FeatureSpec
from xq.features.momentum import MOMENTUM
from xq.features.mtf import context_prefix, join_on_availability
from xq.features.price import PRICE
from xq.features.structure import STRUCTURE
from xq.features.time import TIME
from xq.features.volatility import VOLATILITY
from xq.tracking.db import session_factory
from xq.tracking.models import FeatureSetRecord

#: Code version of the feature-set machinery (the joins, naming and dataset warm-up); part of
#: every dataset id of a configured feature set, next to each feature's own version.
#: 2: the builder reads each timeframe's warm-up bars before the start (C-33 (3), ADR 0067).
FRAMEWORK_VERSION = 2
_FAMILY_FEATURES: tuple[Feature, ...] = (*PRICE, *MOMENTUM, *VOLATILITY, *STRUCTURE, *TIME)


class GatedFeatureError(ConfigError):
    """A model asked for a gated feature whose gate is not admitted (C-33 (4))."""


class FeatureSetChangedError(ConfigError):
    """A feature set version is used with a definition different from the locked one."""


def build_registry(features: Sequence[Feature]) -> Mapping[str, Feature]:
    """An immutable mapping of `features` by name.

    Raises:
        ConfigError: if two features share a name.
    """
    registry: dict[str, Feature] = {}
    for feature in features:
        if feature.name in registry:
            raise ConfigError(f"feature {feature.name!r} is registered twice")
        registry[feature.name] = feature
    return MappingProxyType(registry)


FEATURES: Mapping[str, Feature] = build_registry(_FAMILY_FEATURES)


def feature(name: str, registry: Mapping[str, Feature] = FEATURES) -> Feature:
    """The registered feature `name`.

    Raises:
        ConfigError: if no such feature is registered.
    """
    try:
        return registry[name]
    except KeyError:
        known = ", ".join(sorted(registry)) or "none"
        raise ConfigError(f"unknown feature {name!r}; registered: {known}") from None


def feature_specs(
    definition: FeatureSetConfig, registry: Mapping[str, Feature] = FEATURES
) -> list[FeatureSpec]:
    """The specs of a feature set definition: its features, then its gated features.

    Raises:
        ConfigError: for an unknown feature, invalid parameters, two specs writing the same
            column, a gated feature listed among the features, or an ungated one among the gated.
    """
    specs = []
    for instance in definition.features:
        registered = feature(instance.feature, registry)
        if registered.gate is not None:
            raise ConfigError(
                f"feature {registered.name!r} waits for gate {registered.gate!r} "
                f"({GATES[registered.gate]}); list it under `gated`"
            )
        specs.append(registered.spec(instance))
    for instance in definition.gated:
        registered = feature(instance.feature, registry)
        if registered.gate is None:
            raise ConfigError(f"feature {registered.name!r} has no gate; list it under `features`")
        specs.append(registered.spec(instance))
    columns = [output_prefix(s) + s.column for s in specs]
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        raise ConfigError(f"feature set writes columns {duplicates} more than once")
    return specs


def column_owners(specs: Sequence[FeatureSpec], columns: Sequence[str]) -> dict[str, FeatureSpec]:
    """The spec that wrote each of `columns`: the one whose output name equals it, or else the
    longest output name it extends with ``_<sub>`` (so ``session_vwap_96`` belongs to
    ``session_vwap_96``, not to the multi-column ``session``). Columns no spec wrote are left
    out."""
    named = [(output_prefix(s) + s.column, s) for s in specs]
    owners: dict[str, FeatureSpec] = {}
    for column in columns:
        matches = [(n, s) for n, s in named if column == n or column.startswith(f"{n}_")]
        if matches:
            owners[column] = max(matches, key=lambda m: len(m[0]))[1]
    return owners


def model_inputs(
    features: pd.DataFrame,
    definition: FeatureSetConfig,
    requested: Sequence[str] | None = None,
    *,
    registry: Mapping[str, Feature] = FEATURES,
) -> pd.DataFrame:
    """The columns of a dataset's `features` a model may take as inputs (C-33 (4)).

    By default every column the set's ungated features write; with `requested`, exactly those
    columns. Provenance columns are never inputs, and a gated feature's columns are refused until
    its gate is admitted (none is today).

    Raises:
        GatedFeatureError: if a requested column belongs to a gated feature.
        ConfigError: if a requested column is not in `features` or is a provenance column.
    """
    specs = feature_specs(definition, registry)
    columns = [str(c) for c in features.columns]
    owners = column_owners(specs, [c for c in columns if not c.endswith(PROVENANCE_SUFFIX)])
    gated = {c: s for c, s in owners.items() if s.gate is not None}
    if requested is None:
        return features[[c for c, s in owners.items() if s.gate is None]]
    for column in requested:
        if column in gated:
            gate = gated[column].gate
            assert gate is not None
            raise GatedFeatureError(
                f"{column!r} is gated ({gate}: {GATES[gate]}); it is not a model input until "
                "the gate is admitted"
            )
        if column not in columns or column.endswith(PROVENANCE_SUFFIX):
            raise ConfigError(f"{column!r} is not a feature column of this dataset")
    return features[list(requested)]


def output_prefix(spec: FeatureSpec) -> str:
    """``mtf_<timeframe>_`` for a context-timeframe spec, nothing for a base-timeframe one."""
    return "" if spec.timeframe is None else context_prefix(spec.timeframe)


def compute_feature_set(
    specs: Sequence[FeatureSpec],
    inputs: Inputs,
    context: FeatureContext,
    *,
    base_columns: bool = False,
    registry: Mapping[str, Feature] = FEATURES,
) -> pd.DataFrame:
    """Every spec's columns at each decision time of the base bars (module docstring).

    Raises:
        ConfigError: if a spec names a timeframe the inputs do not carry.
        ValueError: if a column would be written twice.
    """
    base = inputs[BASE_INPUT]
    index = decision_index(base)
    parts = [base_v1(inputs, context)] if base_columns else []
    by_timeframe: dict[Timeframe | None, list[FeatureSpec]] = {}
    for spec in specs:
        timeframe = None if spec.timeframe == context.base_timeframe else spec.timeframe
        by_timeframe.setdefault(timeframe, []).append(spec)
    for timeframe, group in by_timeframe.items():
        if timeframe is None:
            bar_context = BarContext(context.base_timeframe, context.sessions)
            for spec in group:
                computed = feature(spec.name, registry).on_bars(spec, base, bar_context)
                parts.append(computed.set_axis(index, axis=0))
            continue
        if timeframe.value not in inputs:
            raise ConfigError(
                f"feature set computes on {timeframe.value} bars, which the dataset does not load "
                "(add it to the spec's context_timeframes)"
            )
        bars = inputs[timeframe.value]
        bar_context = BarContext(timeframe, context.sessions)
        computed = pd.concat(
            [feature(s.name, registry).on_bars(s, bars, bar_context) for s in group], axis=1
        )
        parts.append(join_on_availability(index, computed, context_prefix(timeframe)))
    if not parts:
        return pd.DataFrame(index=index)
    out = pd.concat(parts, axis=1)
    duplicated = sorted({str(c) for c in out.columns[out.columns.duplicated()]})
    if duplicated:
        raise ValueError(f"feature columns {duplicated} are written twice")
    return out


def definition_payload(
    definition: FeatureSetConfig, registry: Mapping[str, Feature] = FEATURES
) -> dict[str, Any]:
    """What identifies a configured feature set: its definition and the code versions of the
    framework and of every feature it uses."""
    used = sorted({i.feature for i in [*definition.features, *definition.gated]})
    return {
        "definition": definition.model_dump(mode="json"),
        "framework": FRAMEWORK_VERSION,
        "features": {name: feature(name, registry).version for name in used},
    }


def definition_hash(
    definition: FeatureSetConfig, registry: Mapping[str, Feature] = FEATURES
) -> str:
    """SHA-256 of `definition_payload`'s canonical JSON."""
    canonical = json.dumps(
        definition_payload(definition, registry), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lock_feature_set(
    engine: Engine,
    name: str,
    version: str,
    definition: FeatureSetConfig,
    *,
    registry: Mapping[str, Feature] = FEATURES,
) -> str:
    """Record a feature set version's definition on first use; refuse a changed definition.

    Returns:
        The definition hash.

    Raises:
        FeatureSetChangedError: if the version is locked with a different definition.
    """
    digest = definition_hash(definition, registry)
    with session_factory(engine)() as session:
        record = session.get(FeatureSetRecord, (name, version))
        if record is None:
            session.add(
                FeatureSetRecord(
                    name=name,
                    version=version,
                    spec_json=definition_payload(definition, registry),
                    hash=digest,
                    created_at=utc_now(),
                )
            )
            session.commit()
        elif record.hash != digest:
            raise FeatureSetChangedError(
                f"feature set {name}.{version} was locked with a different definition; "
                "give the changed definition a new version"
            )
    return digest


def unchecked_features(cfg: AppConfig, registry: Mapping[str, Feature] = FEATURES) -> list[str]:
    """Registered features that no configured feature set uses, so the leakage suite, which runs
    on every configured spec, would not check them (it fails on any)."""
    used = {
        i.feature
        for versions in cfg.features.values()
        for d in versions.values()
        for i in [*d.features, *d.gated]
    }
    return sorted(set(registry) - used)


def configured(cfg: AppConfig, ref: SetRef) -> FeatureSetConfig | None:
    """The configured definition of `ref`, or None for a built-in set (``base.v1``)."""
    if (ref.name, ref.version) in FEATURE_SETS:
        return None
    return cfg.feature_set_config(ref.name, ref.version)


def resolve_feature_set(cfg: AppConfig, ref: SetRef) -> FeatureSetDef:
    """The `FeatureSetDef` the dataset builder computes for `ref`: a built-in set, or a set of
    ``config/features.yaml`` (module docstring).

    Raises:
        ConfigError: if no such set exists or its definition does not validate.
    """
    definition = configured(cfg, ref)
    if definition is None:
        return FEATURE_SETS[(ref.name, ref.version)]
    specs = feature_specs(definition)

    def compute(inputs: Inputs, context: FeatureContext) -> pd.DataFrame:
        return compute_feature_set(specs, inputs, context, base_columns=definition.base_columns)

    return FeatureSetDef(
        name=ref.name,
        version=ref.version,
        code_version=FRAMEWORK_VERSION,
        compute=compute,
        description=definition.description,
    )


def warmup_bars(cfg: AppConfig, ref: SetRef) -> dict[str, int]:
    """Bars of history each input frame (``base`` or a context timeframe) needs available by a
    dataset's first decision for every feature of set `ref` to be defined there: the longest of
    its features' lookbacks and warm-ups on that frame (C-33 (3), ADR 0067). Empty for a built-in
    set."""
    definition = configured(cfg, ref)
    if definition is None:
        return {}
    needs: dict[str, int] = {}
    for spec in feature_specs(definition):
        name = spec.inputs[0]
        needs[name] = max(needs.get(name, 0), spec.lookback, spec.warmup)
    return needs


def code_versions(cfg: AppConfig, ref: SetRef) -> dict[str, int]:
    """Code versions a dataset of feature set `ref` depends on (part of its id)."""
    definition = configured(cfg, ref)
    if definition is None:
        return {f"features:{ref}": FEATURE_SETS[(ref.name, ref.version)].code_version}
    payload = definition_payload(definition)
    versions = {f"features:{ref}": FRAMEWORK_VERSION}
    versions.update({f"feature:{name}": v for name, v in payload["features"].items()})
    return versions
