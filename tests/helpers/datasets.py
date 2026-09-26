"""Build synthetic datasets end to end: pipeline, quality run, spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from helpers.pipeline import run_pipeline
from xq.core.config import AppConfig
from xq.datasets.spec import DatasetSpec
from xq.quality.validate import validate_source

QUALITY_RUN = "01QRUN0000000000000000DS01"

SPEC: dict[str, Any] = {
    "name": "ds_test",
    "source": "mt5_primary",
    "instrument": "xauusd",
    "base_timeframe": "15m",
    "start": "2024-03-12T00:00:00Z",
    "end": "2024-03-15T20:00:00Z",
    "warmup": "1D",
    "context_timeframes": ["1h", "4h"],
    "feature_set": {"name": "base", "version": "v1"},
}


def validated_pipeline(cfg: AppConfig, *source_dirs: Path, run_id: str = QUALITY_RUN) -> Engine:
    """Ingest, clean, build bars and spreads, then run the quality checks."""
    engine = run_pipeline(cfg, *source_dirs)
    validate_source(cfg, engine, "mt5_primary", run_id=run_id, git_sha="test")
    return engine


def dataset_spec(**changes: Any) -> DatasetSpec:
    return DatasetSpec.model_validate({**SPEC, **changes})
