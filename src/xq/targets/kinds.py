"""The target kinds known to the dataset builder (TGT-001).

A static mapping, filled explicitly here as kinds are implemented.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from types import MappingProxyType

import pandas as pd

from xq.backtest.costs import CostModel
from xq.core.config import AppConfig, TargetSetConfig
from xq.core.errors import ConfigError
from xq.data.calendar import regular_trading_day
from xq.targets.barrier import TRIPLE_BARRIER
from xq.targets.base import TargetKind, TargetSpec
from xq.targets.excursion import EXCURSION
from xq.targets.returns import FORWARD_RETURN
from xq.targets.volatility import REALIZED_VOL
from xq.targets.weights import DERIVED_LABEL

TARGET_KINDS: Mapping[str, TargetKind] = MappingProxyType(
    {
        kind.name: kind
        for kind in (FORWARD_RETURN, REALIZED_VOL, EXCURSION, TRIPLE_BARRIER, DERIVED_LABEL)
    }
)


def target_kind(name: str) -> TargetKind:
    """The kind called `name`.

    Raises:
        ConfigError: if no such kind exists.
    """
    try:
        return TARGET_KINDS[name]
    except KeyError:
        known = ", ".join(sorted(TARGET_KINDS)) or "none"
        raise ConfigError(f"unknown target kind {name!r}; available: {known}") from None


def target_specs(cfg: AppConfig, definition: TargetSetConfig, instrument: str) -> list[TargetSpec]:
    """The target specs of `definition` for `instrument`, ready to compute: expanded by its kind
    over the calendar's regular trading day, with the backtester's own `CostModel` bound when the
    kind prices trades (C-30 (3), ADR 0065). The dataset builder and the leakage harness both
    build specs here.

    Raises:
        ConfigError: for an unknown kind or cost model, or a cost model whose latency or fill
            delay differs from the target set's execution parameters (the label's fills would not
            be the backtester's).
    """
    kind = target_kind(definition.kind)
    specs = kind.expand(definition, regular_trading_day(cfg.sessions_config()))
    name = kind.cost_model(definition)
    if name is None:
        return specs
    if name not in cfg.costs:
        raise ConfigError(f"target set names cost model {name!r}, not in config/costs")
    costs = CostModel.from_config(cfg, instrument, name=name)
    latency = pd.Timedelta(milliseconds=int(definition.params["execution_latency_ms"]))
    delay = pd.Timedelta(seconds=float(definition.params["max_fill_delay_s"]))
    if (latency, delay) != (costs.latency, costs.max_fill_delay):
        raise ConfigError(
            f"cost model {name!r} fills {costs.latency} after a decision within "
            f"{costs.max_fill_delay}; the target set's execution parameters say {latency} and "
            f"{delay}"
        )
    return [dataclasses.replace(spec, costs=costs) for spec in specs]
