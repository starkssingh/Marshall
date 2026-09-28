"""Reproducing a recorded run: ``xq exp reproduce <run_id>`` (EXP-006).

`reproduce_run` rebuilds the run's dataset, repeats the run and compares its metrics:

1. **The original** must have finished, and its kind must have a reproducer (``baseline_board``
   today; other kinds are refused by name until they get one).
2. **The dataset is rebuilt** from the resolved spec ``dataset_versions`` recorded for it
   (`recorded_spec`), by the dataset builder: identical content is confirmed, different content
   raises `DatasetIntegrityError`, and a spec that now builds another dataset id (the code that
   produces the data changed) fails the reproduction.
3. **The run is repeated** in a new run context of kind ``reproduction``, under the original's
   hypothesis, with the original's run configuration and seed, on the rebuilt dataset, with the
   walk-forward prediction cache off so every forecast is recomputed. Configurations the original
   run recorded are not counted as trials again (`RunContext.reproduces`): the same configuration
   on the same data is not a new trial.
4. **Metrics are compared**: every metric the original logged must be logged again with
   ``|reproduced - original| <= atol + rtol * |original|`` (defaults 1e-9 and 1e-6: a rerun on the
   same code and data should be exact; the tolerance allows floating-point differences between
   machines). Metrics that depend on the registry's state rather than on the run (the deflated
   Sharpe ratio, through the family's trial count at the time) are shown but not judged.

5. **The status** (C-24, ADR 0055) is **REPRODUCED** only when the git sha, the application
   config hash and the ``uv.lock`` hash of the reproduction equal the original's **and** every
   judged metric is within tolerance. A sha must identify a commit: an unknown sha or a dirty tree
   (``+dirty``) identifies no code, and a missing lockfile no environment, so neither matches, even
   itself. When any of the three differs or cannot be identified the status is
   **RERUN_DIFFERENT_CODE**: the metrics are still compared and reported, but the run is never
   counted as reproduced. When the code is the same and a judged metric is not within tolerance,
   the status is **NOT_REPRODUCED**, a genuine failure to reproduce. The status and the comparisons
   are written to ``<reports_dir>/reproductions/<reproduction run id>.json`` and recorded as an
   artifact of the reproduction run.

Provenance differences are reported, not refused: code moving on is what a reproduction tests.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine

from xq.core.config import AppConfig
from xq.core.errors import XQError
from xq.core.ids import UNKNOWN_SHA, git_sha
from xq.datasets.builder import build_dataset, recorded_spec
from xq.models.board import BoardConfig, run_baseline_board
from xq.tracking.registry import (
    RunRef,
    RunStatus,
    get_experiment,
    get_metrics,
    get_run,
)
from xq.tracking.runs import MISSING_LOCK, RunContext, experiment_run

REPRODUCTION_KIND = "reproduction"
REPORT_DIR = "reproductions"
DEFAULT_RTOL = 1e-6
DEFAULT_ATOL = 1e-9
Reproducer = Callable[[RunContext, RunRef], None]
#: Provenance fields that must match (and identify code) for a run to count as reproduced.
IDENTITY_FIELDS = ("git_sha", "app_config_hash", "lock_hash")


class ReproductionStatus(StrEnum):
    """The outcome of a reproduction (module docstring, item 5)."""

    REPRODUCED = "REPRODUCED"
    RERUN_DIFFERENT_CODE = "RERUN_DIFFERENT_CODE"
    NOT_REPRODUCED = "NOT_REPRODUCED"


class ReproductionError(XQError):
    """A run cannot be reproduced (unfinished, unknown kind, or its data definition changed)."""


def _reproduce_board(context: RunContext, original: RunRef) -> None:
    config = original.config["run"]
    if original.dataset_id is None:
        raise ReproductionError(f"board run {original.run_id} records no dataset")
    run_baseline_board(
        context,
        original.dataset_id,
        BoardConfig.model_validate(config["board"]),
        targets=list(config["targets"]),
        use_cache=False,
    )


#: Run kind -> how to repeat it inside a new run context.
REPRODUCERS: dict[str, Reproducer] = {"baseline_board": _reproduce_board}
#: Run kind -> suffixes of metric names that depend on the registry's state (shown, not judged).
UNJUDGED_SUFFIXES: dict[str, tuple[str, ...]] = {"baseline_board": ("/dsr",)}


@dataclass(frozen=True)
class Reproduction:
    """The outcome of `reproduce_run` (module docstring)."""

    original: RunRef
    reproduction: RunRef
    dataset_id: str | None
    #: Identity fields that differ or identify nothing: name -> (original, reproduction).
    provenance: dict[str, tuple[str, str]]
    #: One row per original metric: ``name``, ``fold_id``, ``original``, ``reproduced``,
    #: ``judged`` and ``within`` (tolerance).
    comparisons: pd.DataFrame
    rtol: float
    atol: float
    #: The status file written for the reproduction run.
    report_path: Path | None = None

    @property
    def mismatches(self) -> pd.DataFrame:
        """Judged metrics outside the tolerance or missing from the reproduction."""
        rows = self.comparisons
        return rows.loc[rows["judged"] & ~rows["within"]]

    @property
    def metrics_within(self) -> bool:
        """At least one metric was judged and every judged metric is within tolerance."""
        return bool(self.comparisons["judged"].any()) and self.mismatches.empty

    @property
    def same_code(self) -> bool:
        """Git sha, config hash and lock hash match and identify the code and environment."""
        return not self.provenance

    @property
    def status(self) -> ReproductionStatus:
        """REPRODUCED, RERUN_DIFFERENT_CODE or NOT_REPRODUCED (module docstring, item 5)."""
        if not self.same_code:
            return ReproductionStatus.RERUN_DIFFERENT_CODE
        if self.metrics_within:
            return ReproductionStatus.REPRODUCED
        return ReproductionStatus.NOT_REPRODUCED

    @property
    def reproduced(self) -> bool:
        """Whether the run counts as reproduced: only with status REPRODUCED."""
        return self.status is ReproductionStatus.REPRODUCED


def reproduce_run(
    cfg: AppConfig,
    engine: Engine,
    run_id: str,
    *,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
    exploratory: bool = False,
) -> Reproduction:
    """Rebuild `run_id`'s dataset, repeat the run and compare its metrics (module docstring).

    Args:
        exploratory: Allow a dirty git tree; the reproduction is then not confirmatory.

    Raises:
        ReproductionError: for an unfinished run, a kind without a reproducer, or a dataset spec
            that now builds another dataset.
        DatasetIntegrityError: if the rebuilt dataset's content differs from the recorded one.
        RunContextError: for a confirmatory reproduction on a dirty tree.
    """
    if rtol < 0 or atol < 0:
        raise ValueError("tolerances must not be negative")
    original = get_run(engine, run_id)
    if original.status is not RunStatus.FINISHED:
        raise ReproductionError(f"run {run_id} is {original.status}; only finished runs reproduce")
    reproducer = REPRODUCERS.get(original.kind)
    if reproducer is None:
        raise ReproductionError(
            f"runs of kind {original.kind!r} have no reproducer yet; reproducible kinds: "
            f"{sorted(REPRODUCERS)}"
        )
    if original.dataset_id is not None:
        repo = cfg.paths.resolve(cfg.paths.root)
        rebuilt = build_dataset(
            cfg, engine, recorded_spec(engine, original.dataset_id), git_sha=git_sha(repo)
        )
        if rebuilt.dataset_id != original.dataset_id:
            raise ReproductionError(
                f"the recorded spec of {original.dataset_id} now builds {rebuilt.dataset_id}: the "
                "code that produces the dataset changed, so the run cannot be reproduced on it"
            )
    experiment = get_experiment(engine, original.experiment_id)
    with experiment_run(
        cfg,
        engine,
        experiment.hypothesis_id,
        {"reproduces": run_id, "run": original.config.get("run", {})},
        kind=REPRODUCTION_KIND,
        seed=original.seed,
        dataset_id=original.dataset_id,
        exploratory=exploratory,
        title=f"reproduction of {run_id}",
        reproduces=run_id,
    ) as context:
        reproducer(context, original)
        repeated = get_run(engine, context.run_id)
        result = Reproduction(
            original,
            repeated,
            original.dataset_id,
            _provenance(original, repeated),
            _compare(
                engine, original, repeated, rtol, atol, UNJUDGED_SUFFIXES.get(original.kind, ())
            ),
            rtol,
            atol,
        )
        path = _write_report(cfg, result)
        context.log_artifact(path, kind="reproduction")
    return Reproduction(
        result.original,
        get_run(engine, context.run_id),
        result.dataset_id,
        result.provenance,
        result.comparisons,
        rtol,
        atol,
        path,
    )


def _write_report(cfg: AppConfig, result: Reproduction) -> Path:
    """The status, provenance and comparisons as JSON, for the registry and the gate report."""
    directory = cfg.paths.resolve(cfg.paths.reports_dir) / REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.reproduction.run_id}.json"
    rows = [
        {key: None if isinstance(v, float) and math.isnan(v) else v for key, v in row.items()}
        for row in result.comparisons.to_dict(orient="records")
    ]
    payload = {
        "original_run_id": result.original.run_id,
        "reproduction_run_id": result.reproduction.run_id,
        "status": result.status.value,
        "counts_as_reproduced": result.reproduced,
        "identity": {
            name: {
                "original": before,
                "reproduction": now,
                "matches": name not in result.provenance,
            }
            for name, (before, now) in _identity(result.original, result.reproduction).items()
        },
        "rtol": result.rtol,
        "atol": result.atol,
        "comparisons": rows,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def _compare(
    engine: Engine,
    original: RunRef,
    repeated: RunRef,
    rtol: float,
    atol: float,
    unjudged: tuple[str, ...],
) -> pd.DataFrame:
    again = {(m.name, m.fold_id): m.value for m in get_metrics(engine, repeated.run_id)}
    rows = []
    for metric in get_metrics(engine, original.run_id):
        value = again.get((metric.name, metric.fold_id), math.nan)
        rows.append(
            {
                "name": metric.name,
                "fold_id": metric.fold_id,
                "original": metric.value,
                "reproduced": value,
                "judged": not metric.name.endswith(unjudged),
                "within": abs(value - metric.value) <= atol + rtol * abs(metric.value),
            }
        )
    columns = ["name", "fold_id", "original", "reproduced", "judged", "within"]
    return pd.DataFrame(rows, columns=columns)


def _identity(original: RunRef, repeated: RunRef) -> dict[str, tuple[str, str]]:
    return {
        "git_sha": (original.git_sha, repeated.git_sha),
        "app_config_hash": (
            str(original.config.get("app_config_hash")),
            str(repeated.config.get("app_config_hash")),
        ),
        "lock_hash": (original.lock_hash, repeated.lock_hash),
    }


def _identifies(name: str, value: str) -> bool:
    """Whether a provenance value identifies code or environment at all."""
    if name == "git_sha":
        return value != UNKNOWN_SHA and not value.endswith("+dirty")
    if name == "lock_hash":
        return value != MISSING_LOCK
    return value != "None"


def _provenance(original: RunRef, repeated: RunRef) -> dict[str, tuple[str, str]]:
    """Identity fields that differ, or that do not identify anything on either side."""
    return {
        name: pair
        for name, pair in _identity(original, repeated).items()
        if pair[0] != pair[1] or not (_identifies(name, pair[0]) and _identifies(name, pair[1]))
    }


def describe(result: Reproduction) -> list[str]:
    """Lines for the CLI: the verdict, provenance differences and every mismatch."""
    rows = result.comparisons
    judged = int(rows["judged"].sum())
    lines = [
        f"run {result.original.run_id} ({result.original.kind}) -> reproduction "
        f"{result.reproduction.run_id}: {result.status.value}; "
        f"{judged - len(result.mismatches)} of {judged} "
        f"judged metrics within rtol {result.rtol:g}, atol {result.atol:g}; "
        f"{len(rows) - judged} not judged (depend on the registry's state)",
    ]
    if result.status is ReproductionStatus.RERUN_DIFFERENT_CODE:
        lines.append(
            "not counted as reproduced: git sha, config hash and lock hash must match and "
            "identify the code"
        )
    if result.dataset_id is not None:
        lines.append(f"dataset {result.dataset_id} rebuilt with identical content")
    for name, (before, now) in result.provenance.items():
        same = "unidentified" if before == now else "differs"
        lines.append(f"provenance {same}: {name} {before} -> {now}")
    for row in result.mismatches.to_dict(orient="records"):
        fold = f" [{row['fold_id']}]" if row["fold_id"] else ""
        lines.append(f"mismatch: {row['name']}{fold}: {row['original']!r} -> {row['reproduced']!r}")
    if result.report_path is not None:
        lines.append(f"status file: {result.report_path}")
    return lines
