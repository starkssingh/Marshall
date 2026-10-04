"""The target kinds known to the dataset builder (TGT-001).

A static mapping, filled explicitly here as kinds are implemented.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from xq.core.errors import ConfigError
from xq.targets.base import TargetKind
from xq.targets.excursion import EXCURSION
from xq.targets.returns import FORWARD_RETURN
from xq.targets.volatility import REALIZED_VOL

TARGET_KINDS: Mapping[str, TargetKind] = MappingProxyType(
    {kind.name: kind for kind in (FORWARD_RETURN, REALIZED_VOL, EXCURSION)}
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
