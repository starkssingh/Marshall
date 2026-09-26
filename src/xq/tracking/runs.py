"""The experiment run context (EXP-003).

Every research result must come from inside ``experiment_run``::

    with experiment_run(
        cfg, engine, "H-0001", {"lookback": 20}, kind="baseline", seed=7, dataset_id=ref.dataset_id
    ) as run:
        ...
        run.log_metric("sharpe_net", 0.41, fold_id="2024Q1")

On entry it captures what is needed to reproduce the run — git sha, whether the tree is clean,
a hash of the run's configuration together with the application config hash, the dataset id
(verified against its manifest), the SHA-256 of ``uv.lock``, the seed and the host — seeds all
randomness from `seed` (`xq.core.seeds`) and records a ``running`` run on the hypothesis's open
experiment. On exit the run is marked ``finished``, or ``failed`` if the block raised.

A **confirmatory** run (the default) is refused unless the git tree is clean and identifiable and
``uv.lock`` exists: results that cite code must be able to point at a commit. ``exploratory=True``
allows a dirty tree but records the run as non-confirmatory, so it can never be cited as evidence.
"""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import Engine

from xq.core.config import AppConfig, config_hash
from xq.core.errors import XQError
from xq.core.ids import UNKNOWN_SHA, git_is_dirty, git_sha
from xq.core.logging import get_logger
from xq.core.seeds import make_rng, set_global_seed
from xq.data.raw_store import sha256_file
from xq.datasets.builder import verify_dataset
from xq.tracking import registry
from xq.tracking.registry import RunRef, RunStatus
from xq.tracking.trials import record_trial

LOCK_FILE = "uv.lock"
MISSING_LOCK = "missing"

log = get_logger(__name__)


class RunContextError(XQError):
    """A run cannot start: its provenance would be incomplete for the kind of run requested."""


@dataclass
class RunContext:
    """Handle to a running experiment run."""

    run: RunRef
    cfg: AppConfig
    engine: Engine
    rng: np.random.Generator

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def confirmatory(self) -> bool:
        return self.run.confirmatory

    def log_metric(self, name: str, value: float, *, fold_id: str | None = None) -> None:
        """Record a metric (optionally for one walk-forward fold)."""
        registry.log_metric(self.engine, self.run_id, name, value, fold_id=fold_id)

    def log_artifact(self, path: Path, *, kind: str) -> registry.ArtifactRecord:
        """Record a file this run produced, with its SHA-256."""
        return registry.log_artifact(self.engine, self.run_id, path, kind=kind)

    def record_trial(
        self,
        *,
        family_id: str,
        config: Mapping[str, Any],
        evaluated_on_test: bool,
        sharpe: float | None = None,
        returns: pd.Series | None = None,
    ) -> str:
        """Record one evaluated configuration (see `xq.tracking.trials.record_trial`)."""
        return record_trial(
            self,
            family_id=family_id,
            config=config,
            evaluated_on_test=evaluated_on_test,
            sharpe=sharpe,
            returns=returns,
        )


def run_config_hash(cfg: AppConfig, config: Mapping[str, Any]) -> str:
    """16-hex hash of the run's configuration together with the application config hash."""
    payload = {"app": config_hash(cfg), "run": config}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@contextmanager
def experiment_run(
    cfg: AppConfig,
    engine: Engine,
    hypothesis_id: str,
    config: Mapping[str, Any],
    *,
    kind: str,
    seed: int,
    dataset_id: str | None = None,
    exploratory: bool = False,
    title: str | None = None,
) -> Iterator[RunContext]:
    """Record a run of `hypothesis_id` around the enclosed block (see the module docstring).

    Raises:
        RunContextError: for a confirmatory run on a dirty or unidentifiable git tree, or without
            ``uv.lock``.
        RegistryError: if the hypothesis is not registered.
        NoDatasetDataError, DatasetIntegrityError: if `dataset_id` is missing or altered.
    """
    repo = cfg.paths.resolve(cfg.paths.root)
    sha = git_sha(repo)
    dirty = git_is_dirty(repo)
    lock = repo / LOCK_FILE
    lock_hash = sha256_file(lock) if lock.is_file() else MISSING_LOCK
    if not exploratory:
        problems = []
        if sha == UNKNOWN_SHA or dirty is None:
            problems.append("the git commit cannot be determined")
        elif dirty:
            problems.append("the git tree has uncommitted changes")
        if lock_hash == MISSING_LOCK:
            problems.append(f"{LOCK_FILE} is missing")
        if problems:
            raise RunContextError(
                "a confirmatory run needs a clean, identifiable git tree and a lockfile: "
                + "; ".join(problems)
                + ". Commit first, or pass exploratory=True (the run is then non-confirmatory)"
            )
    registry.get_hypothesis(engine, hypothesis_id)  # fail before anything is recorded
    if dataset_id is not None:
        verify_dataset(cfg, dataset_id)

    set_global_seed(seed)
    experiment = registry.open_experiment_for(engine, hypothesis_id, title or f"{hypothesis_id}")
    run = registry.start_run(
        engine,
        experiment.experiment_id,
        kind=kind,
        confirmatory=not exploratory,
        git_sha=sha if not dirty else f"{sha}+dirty",
        config_hash=run_config_hash(cfg, config),
        config={"run": dict(config), "app_config_hash": config_hash(cfg)},
        dataset_id=dataset_id,
        lock_hash=lock_hash,
        seed=seed,
        host=socket.gethostname(),
    )
    context = RunContext(run, cfg, engine, make_rng(seed))
    log.info(
        "experiment_run_started",
        experiment_run_id=run.run_id,
        hypothesis_id=hypothesis_id,
        confirmatory=run.confirmatory,
    )
    with structlog.contextvars.bound_contextvars(experiment_run_id=run.run_id):
        try:
            yield context
        except BaseException:
            registry.finish_run(engine, run.run_id, RunStatus.FAILED)
            log.warning("experiment_run_failed", experiment_run_id=run.run_id)
            raise
    context.run = registry.finish_run(engine, run.run_id, RunStatus.FINISHED)
    log.info("experiment_run_finished", experiment_run_id=run.run_id)
