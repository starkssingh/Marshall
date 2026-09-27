"""Experiment conclusions and the research log (EXP-005).

An experiment is closed only with a written conclusion: a **verdict** — supported, rejected or
inconclusive — and the five fields **Observed**, **Evidence**, **Interpretation**,
**Limitations** and **Action**, none of them empty (``experiments/conclusions/TEMPLATE.yaml``).
`close_experiment` is the only way to close one, so closing without a conclusion fails. It also
refuses to close:

- an experiment that is already closed (the registry is append-only; a revised view is a new
  experiment with its own conclusion);
- an experiment with a run still running;
- a ``supported`` verdict without a finished confirmatory run — exploratory runs can never be
  cited as evidence (EXP-003) — and a ``rejected`` verdict without any finished run.

Closing stores the conclusion, marks the experiment closed with its verdict, and appends an entry
to the research log (``paths.research_log``, ``docs/research/log.md``) with the hypothesis
version, the runs and the five fields. The database change is committed only after the entry is
written, so a failed write leaves the experiment open.

`unconcluded_experiments` is the audit: every experiment still open, i.e. without a conclusion.
The plan requires it to be empty at the end of each sprint (``xq exp audit``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy import Engine, select

from xq.core.config import AppConfig
from xq.core.errors import XQError
from xq.core.time import utc_now
from xq.tracking.db import session_factory
from xq.tracking.models import Conclusion, Experiment, Hypothesis, Run
from xq.tracking.registry import ExperimentRef, ExperimentStatus, RunStatus, list_experiments

FIELDS = ("observed", "evidence", "interpretation", "limitations", "action")
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConclusionError(XQError):
    """An experiment cannot be closed with this conclusion."""


class Verdict(StrEnum):
    SUPPORTED = "supported"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class ConclusionDoc(BaseModel):
    """A written conclusion: the verdict and the five required fields (none may be empty)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    observed: Text
    evidence: Text
    interpretation: Text
    limitations: Text
    action: Text


@dataclass(frozen=True)
class ConclusionRecord:
    """A stored conclusion."""

    experiment_id: str
    verdict: Verdict
    observed: str
    evidence: str
    interpretation: str
    limitations: str
    action: str
    created_at: pd.Timestamp


def parse_conclusion(data: Any) -> ConclusionDoc:
    """Validate a conclusion mapping.

    Raises:
        ConclusionError: if the verdict is unknown or a field is missing, empty or unexpected.
    """
    try:
        return ConclusionDoc.model_validate(data)
    except ValidationError as exc:
        raise ConclusionError(
            "a conclusion needs a verdict (supported, rejected, inconclusive) and non-empty "
            f"{', '.join(FIELDS)}:\n{exc}"
        ) from exc


def load_conclusion(path: Path) -> ConclusionDoc:
    """Read and validate a conclusion YAML file (see the template)."""
    if not path.is_file():
        raise ConclusionError(f"conclusion file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConclusionError(f"cannot parse {path}: {exc}") from exc
    return parse_conclusion(data)


def close_experiment(
    cfg: AppConfig, engine: Engine, experiment_id: str, conclusion: ConclusionDoc
) -> ConclusionRecord:
    """Close `experiment_id` with `conclusion` and append it to the research log.

    Raises:
        ConclusionError: if the experiment is missing or closed, a run is still running, or the
            runs cannot support the verdict (see the module docstring).
    """
    log_path = cfg.paths.resolve(cfg.paths.research_log)
    with session_factory(engine)() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise ConclusionError(f"experiment {experiment_id} not found")
        if experiment.status != ExperimentStatus.OPEN.value:
            raise ConclusionError(
                f"experiment {experiment_id} is already {experiment.status}; conclusions are "
                "never rewritten — open a new experiment instead"
            )
        runs = session.scalars(
            select(Run).where(Run.experiment_id == experiment_id).order_by(Run.started_at)
        ).all()
        _check_runs(experiment_id, conclusion.verdict, runs)
        hypothesis = session.get(
            Hypothesis, (experiment.hypothesis_id, experiment.hypothesis_version)
        )
        now = utc_now()
        record = Conclusion(
            experiment_id=experiment_id,
            verdict=conclusion.verdict.value,
            observed=conclusion.observed,
            evidence=conclusion.evidence,
            interpretation=conclusion.interpretation,
            limitations=conclusion.limitations,
            action=conclusion.action,
            created_at=now,
        )
        session.add(record)
        experiment.status = ExperimentStatus.CLOSED.value
        experiment.verdict = conclusion.verdict.value
        experiment.closed_at = now
        session.flush()
        entry = log_entry(experiment, hypothesis, runs, conclusion, now)
        _append(log_path, entry)
        session.commit()
        return _record(record)


def get_conclusion(engine: Engine, experiment_id: str) -> ConclusionRecord:
    """The conclusion of a closed experiment."""
    with session_factory(engine)() as session:
        record = session.get(Conclusion, experiment_id)
        if record is None:
            raise ConclusionError(f"experiment {experiment_id} has no conclusion")
        return _record(record)


def unconcluded_experiments(engine: Engine) -> list[ExperimentRef]:
    """Every experiment without a conclusion (still open), oldest first: the EXP-005 audit."""
    return [e for e in list_experiments(engine) if e.status is ExperimentStatus.OPEN]


def log_entry(
    experiment: Experiment,
    hypothesis: Hypothesis | None,
    runs: Sequence[Run],
    conclusion: ConclusionDoc,
    closed_at: pd.Timestamp,
) -> str:
    """The research-log entry of a closed experiment (Markdown)."""
    title = hypothesis.title if hypothesis is not None else "(hypothesis not found)"
    confirmatory = sum(1 for r in runs if r.confirmatory)
    lines = [
        f"## {closed_at.date()} — {experiment.hypothesis_id} v{experiment.hypothesis_version}: "
        f"{title} — {conclusion.verdict.value}",
        "",
        f"- **Experiment:** {experiment.experiment_id} ({experiment.title}), opened "
        f"{experiment.created_at}, closed {closed_at}",
        f"- **Runs:** {len(runs)} ({confirmatory} confirmatory)",
    ]
    for run in runs:
        kind = "confirmatory" if run.confirmatory else "exploratory"
        dataset = f", dataset {run.dataset_id}" if run.dataset_id else ""
        lines.append(
            f"  - {run.run_id}: {run.kind}, {kind}, {run.status}, git {run.git_sha}{dataset}"
        )
    lines.append(f"- **Verdict:** {conclusion.verdict.value}")
    for name in FIELDS:
        text = str(getattr(conclusion, name)).replace("\n", "\n  ")
        lines.append(f"- **{name.capitalize()}:** {text}")
    return "\n".join(lines) + "\n"


def _check_runs(experiment_id: str, verdict: Verdict, runs: Sequence[Run]) -> None:
    running = [r.run_id for r in runs if r.status == RunStatus.RUNNING.value]
    if running:
        raise ConclusionError(
            f"experiment {experiment_id} has runs still running: {', '.join(running)}"
        )
    finished = [r for r in runs if r.status == RunStatus.FINISHED.value]
    if verdict is Verdict.SUPPORTED and not any(r.confirmatory for r in finished):
        raise ConclusionError(
            f"a supported verdict needs a finished confirmatory run; experiment {experiment_id} "
            "has none (exploratory runs are never evidence)"
        )
    if verdict is Verdict.REJECTED and not finished:
        raise ConclusionError(
            f"a rejected verdict needs a finished run; experiment {experiment_id} has none"
        )


def _append(path: Path, entry: str) -> None:
    if not path.is_file():
        raise ConclusionError(
            f"research log {path} not found; create it (docs/research/log.md) before closing"
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + entry)


def _record(r: Conclusion) -> ConclusionRecord:
    return ConclusionRecord(
        r.experiment_id,
        Verdict(r.verdict),
        r.observed,
        r.evidence,
        r.interpretation,
        r.limitations,
        r.action,
        r.created_at,
    )
