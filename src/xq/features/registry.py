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
from xq.datasets.asof import asof_join
from xq.datasets.base_features import (
    BASE_INPUT,
    DECISION_TIME,
    FEATURE_SETS,
    FeatureContext,
    FeatureSetDef,
    base_v1,
    decision_index,
)
from xq.datasets.leakage import Inputs
from xq.datasets.spec import SetRef
from xq.features.base import AVAILABLE_AT, BarContext, Feature, FeatureSpec
from xq.features.price import PRICE
from xq.tracking.db import session_factory
from xq.tracking.models import FeatureSetRecord

#: Code version of the feature-set machinery (the joins and naming); part of every dataset id
#: of a configured feature set, next to each feature's own version.
FRAMEWORK_VERSION = 1
MTF_PREFIX = "mtf_"
_FAMILY_FEATURES: tuple[Feature, ...] = (*PRICE,)


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
    """The specs of a feature set definition, in its order.

    Raises:
        ConfigError: for an unknown feature, invalid parameters or two specs writing the same
            column.
    """
    specs = [feature(i.feature, registry).spec(i) for i in definition.features]
    columns = [output_prefix(s) + s.column for s in specs]
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        raise ConfigError(f"feature set writes columns {duplicates} more than once")
    return specs


def output_prefix(spec: FeatureSpec) -> str:
    """``mtf_<timeframe>_`` for a context-timeframe spec, nothing for a base-timeframe one."""
    return "" if spec.timeframe is None else f"{MTF_PREFIX}{spec.timeframe.value}_"


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
    out = base_v1(inputs, context) if base_columns else pd.DataFrame(index=index)
    by_timeframe: dict[Timeframe | None, list[FeatureSpec]] = {}
    for spec in specs:
        timeframe = None if spec.timeframe == context.base_timeframe else spec.timeframe
        by_timeframe.setdefault(timeframe, []).append(spec)
    for timeframe, group in by_timeframe.items():
        if timeframe is None:
            bar_context = BarContext(context.base_timeframe, context.sessions)
            for spec in group:
                computed = feature(spec.name, registry).on_bars(spec, base, bar_context)
                _add(out, computed.set_axis(index, axis=0))
            continue
        if timeframe.value not in inputs:
            raise ConfigError(
                f"feature set computes on {timeframe.value} bars, which the dataset does not load "
                "(add it to the spec's context_timeframes)"
            )
        bars = inputs[timeframe.value]
        bar_context = BarContext(timeframe, context.sessions)
        parts = [feature(s.name, registry).on_bars(s, bars, bar_context) for s in group]
        right = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=bars.index)
        right[AVAILABLE_AT] = right.index
        joined = asof_join(
            pd.DataFrame({DECISION_TIME: index}),
            right.reset_index(drop=True),
            on_right=AVAILABLE_AT,
            prefix=f"{MTF_PREFIX}{timeframe.value}_",
        )
        _add(out, joined.drop(columns=DECISION_TIME).set_axis(index, axis=0))
    return out


def _add(out: pd.DataFrame, columns: pd.DataFrame) -> None:
    clashes = sorted(set(out.columns) & set(columns.columns))
    if clashes:
        raise ValueError(f"feature columns {clashes} are written twice")
    for name in columns.columns:
        out[name] = columns[name].to_numpy()


def definition_payload(
    definition: FeatureSetConfig, registry: Mapping[str, Feature] = FEATURES
) -> dict[str, Any]:
    """What identifies a configured feature set: its definition and the code versions of the
    framework and of every feature it uses."""
    used = sorted({i.feature for i in definition.features})
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
        for i in d.features
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


def code_versions(cfg: AppConfig, ref: SetRef) -> dict[str, int]:
    """Code versions a dataset of feature set `ref` depends on (part of its id)."""
    definition = configured(cfg, ref)
    if definition is None:
        return {f"features:{ref}": FEATURE_SETS[(ref.name, ref.version)].code_version}
    payload = definition_payload(definition)
    versions = {f"features:{ref}": FRAMEWORK_VERSION}
    versions.update({f"feature:{name}": v for name, v in payload["features"].items()})
    return versions
