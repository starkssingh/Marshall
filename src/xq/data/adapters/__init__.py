"""Source adapters (DATA-003). `build_adapter` returns the adapter a source is configured with."""

from __future__ import annotations

from xq.core.config import AppConfig
from xq.data.adapters.base import (
    TICK_SCHEMA,
    RawFileRef,
    SourceAdapter,
    discover_files,
    validate_tick_frame,
)
from xq.data.adapters.mt5 import Mt5TickAdapter

__all__ = [
    "TICK_SCHEMA",
    "Mt5TickAdapter",
    "RawFileRef",
    "SourceAdapter",
    "build_adapter",
    "discover_files",
    "validate_tick_frame",
]


def build_adapter(cfg: AppConfig, source_id: str) -> SourceAdapter:
    """Instantiate the adapter declared for `source_id`."""
    source = cfg.source(source_id)
    if source.adapter == "mt5_ticks":
        return Mt5TickAdapter(source_id, source)
    raise AssertionError(f"unhandled adapter {source.adapter!r}")  # pragma: no cover
