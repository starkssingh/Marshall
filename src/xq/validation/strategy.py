"""`xq validate-strategy <run_id>`: the combined significance and robustness report of a recorded
strategy (Phase 17's acceptance, ROB-008), and `xq robustness simulate`, which records the
known-truth simulated strategies it is proven on.

**Validating a run.** `validate_run` works as follows.

1. The recorded run must have finished. Its kind must have a **subject adapter** that rebuilds
   the strategy and its family from the run's recorded configuration:
   ``simulated_strategy`` (below) today. Other kinds are refused by name until they get one.
2. The family's trials are read from the registry as the gates count them (EXP-004): the family
   is the hypothesis's, and the count is effective or raw per ``conventions.trial_count``. The
   slices, and any declaration that the strategy's parameters were fixed a priori (C-25), are
   read from the locked hypothesis version the run tested (ROB-006). A run that chose its
   strategy's parameters from a grid is refused under a hypothesis declaring them fixed a
   priori: the declaration would be false.
3. A new run of kind ``validation`` under the same hypothesis holds everything. It computes
   `validate_strategy` with the configured risk profile and writes ``report.md`` and
   ``report.json`` under ``<reports_dir>/validation/<run_id>/<validation run id>/`` as artifacts.
   It records every statistical test in ``stat_tests`` and every robustness measure in
   ``robustness_results``, and logs the verdicts as metrics.
4. **A validation run records no trials.** It selects nothing: the candidate was chosen by the
   run it validates, whose trials are already counted. The perturbed, stressed and delayed
   variants are diagnostics of that candidate. A configuration picked from among them would be a
   new selection and must go through a new run, which counts it.

**Simulated runs** (`record_simulated_run`) record a known-truth simulated strategy
(`xq.robustness.simulated`) as a run of kind ``simulated_strategy``. Every configuration of its
family becomes a trial of the hypothesis's family, with its daily returns, as the baseline
board's strategies are. Such runs are always exploratory (never confirmatory) and are labelled
synthetic in every report: they exist to prove the validation machinery, never as evidence.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine

from xq.core.config import AppConfig
from xq.core.errors import XQError
from xq.core.seeds import derive_seed
from xq.core.time import trading_day_bounds
from xq.risk.engine import RiskEngine
from xq.robustness.simulated import SimulationSpec, simulated_family_returns, simulated_subject
from xq.robustness.slicing import run_slices
from xq.robustness.subject import StrategySubject, TrialSummary
from xq.tracking import registry
from xq.tracking.hypotheses import fixed_parameters_source
from xq.tracking.registry import RunRef, RunStatus
from xq.tracking.runs import experiment_run
from xq.tracking.trials import trial_count
from xq.validation.report import StrategyValidation, validate_strategy
from xq.validation.sharpe import sharpe_ratio

SIMULATED_KIND = "simulated_strategy"
VALIDATION_KIND = "validation"
REPORT_DIR = "validation"
#: Run kinds `validate_run` can rebuild a strategy from.
VALIDATABLE_KINDS = (SIMULATED_KIND,)


class StrategyValidationError(XQError):
    """A run cannot be validated (unfinished, or a kind without a subject adapter)."""


@dataclass(frozen=True)
class ValidationRun:
    """The outcome of `validate_run`."""

    validation: StrategyValidation
    run: RunRef
    report_dir: Path


def record_simulated_run(
    cfg: AppConfig, engine: Engine, spec: SimulationSpec, *, hypothesis_id: str
) -> RunRef:
    """Record a simulated strategy's family as a run of `hypothesis_id` (module docstring)."""
    family = simulated_family_returns(spec)
    root = math.sqrt(cfg.gate_periods_per_year())
    starts = pd.DatetimeIndex([trading_day_bounds(d)[0] for d in family.index])
    with experiment_run(
        cfg,
        engine,
        hypothesis_id,
        {"simulation": spec.model_dump(mode="json")},
        kind=SIMULATED_KIND,
        seed=spec.seed,
        exploratory=True,  # synthetic: never confirmatory
        title=f"simulated {spec.truth} strategy, seed {spec.seed}",
    ) as run:
        family_id = _family_id(engine, run.run)
        best = -math.inf
        for name in family.columns:
            values = family[name].to_numpy()
            sharpe = sharpe_ratio(values) * root
            best = max(best, sharpe)
            run.record_trial(
                family_id=family_id,
                config={"simulation": spec.model_dump(mode="json"), "configuration": name},
                evaluated_on_test=True,
                sharpe=sharpe if math.isfinite(sharpe) else None,
                returns=pd.Series(values, index=starts),
            )
        run.log_metric("simulated/candidate_sharpe", best)
    return registry.get_run(engine, run.run_id)


def subject_for_run(
    cfg: AppConfig, engine: Engine, run: RunRef, *, strategy: str | None = None
) -> tuple[StrategySubject, str]:
    """The strategy a run recorded, as a validation subject, and its trial family.

    Raises:
        StrategyValidationError: for a kind without a subject adapter, or a strategy the run
            did not select.
    """
    family_id = _family_id(engine, run)
    stats = trial_count(cfg, engine, family_id)
    conventions = cfg.gates_config().conventions
    trials = TrialSummary(
        n_raw=stats.n_trials,
        n_effective=float(stats.effective_n),
        sharpe_variance=float(stats.sharpe_variance or 0.0),
        gated=conventions.trial_count,
    )
    experiment = registry.get_experiment(engine, run.experiment_id)
    fixed = fixed_parameters_source(engine, experiment.hypothesis_id, experiment.hypothesis_version)
    if run.kind == SIMULATED_KIND:
        spec = SimulationSpec.model_validate(run.config["run"]["simulation"])
        if fixed is not None:
            raise StrategyValidationError(
                f"hypothesis {experiment.hypothesis_id} declares its parameters fixed a priori, "
                f"but run {run.run_id} chose them from a grid of {stats.n_trials} "
                "configurations: the neighbourhood gate applies to them"
            )
        subject = simulated_subject(
            spec,
            capital=cfg.backtest_config().capital_usd,
            periods_per_year=cfg.gate_periods_per_year(),
            trials=trials,
            slices=run_slices(engine, run.run_id),
            sessions=cfg.sessions_config(),
        )
        if strategy is not None and strategy != subject.name:
            raise StrategyValidationError(
                f"run {run.run_id} selected {subject.name!r}, not {strategy!r}"
            )
        return subject, family_id
    raise StrategyValidationError(
        f"runs of kind {run.kind!r} have no subject adapter yet; validatable kinds: "
        f"{list(VALIDATABLE_KINDS)}"
    )


def validate_run(
    cfg: AppConfig,
    engine: Engine,
    run_id: str,
    *,
    strategy: str | None = None,
    exploratory: bool = False,
) -> ValidationRun:
    """Validate the strategy `run_id` recorded, inside a validation run (module docstring).

    Args:
        strategy: The strategy to validate, when the run holds several (a board).
        exploratory: Allow a dirty git tree; the validation run is then not confirmatory.

    Raises:
        StrategyValidationError: for an unfinished run or a kind without a subject adapter.
        RunContextError: for a confirmatory validation on a dirty tree.
    """
    original = registry.get_run(engine, run_id)
    if original.status is not RunStatus.FINISHED:
        raise StrategyValidationError(
            f"run {run_id} is {original.status}; only finished runs can be validated"
        )
    subject, family_id = subject_for_run(cfg, engine, original, strategy=strategy)
    experiment = registry.get_experiment(engine, original.experiment_id)
    with experiment_run(
        cfg,
        engine,
        experiment.hypothesis_id,
        {"validates": run_id, "strategy": subject.name},
        kind=VALIDATION_KIND,
        seed=original.seed,
        dataset_id=original.dataset_id,
        exploratory=exploratory,
        title=f"validation of {subject.name} from {run_id}",
    ) as run:
        validation = validate_strategy(
            subject,
            family_id=family_id,
            gates=cfg.gates_config(),
            settings=cfg.validation_config(),
            risk_engine=RiskEngine.from_config(cfg),
            seed=derive_seed(original.seed, "validation", subject.name),
        )
        directory = cfg.paths.resolve(cfg.paths.reports_dir) / REPORT_DIR / run_id / run.run_id
        directory.mkdir(parents=True, exist_ok=True)
        markdown = directory / "report.md"
        markdown.write_text(validation.markdown() + "\n", encoding="utf-8")
        payload = directory / "report.json"
        payload.write_text(
            json.dumps(validation.to_json(), indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        run.log_artifact(markdown, kind="validation_report")
        run.log_artifact(payload, kind="validation_json")
        registry.record_stat_tests(
            engine, run.run_id, [asdict(t) for t in validation.significance.stat_tests()]
        )
        registry.record_robustness_results(
            engine, run.run_id, [asdict(r) for r in validation.robustness.results()]
        )
        run.log_metric("validation/R1_pass", float(validation.verdict("R1") == "pass"))
        run.log_metric("validation/R2_pass", float(validation.verdict("R2") == "pass"))
        score = validation.robustness.score
        if math.isfinite(score):
            run.log_metric("validation/robustness_score", score)
    return ValidationRun(validation, registry.get_run(engine, run.run_id), directory)


def _family_id(engine: Engine, run: RunRef) -> str:
    experiment = registry.get_experiment(engine, run.experiment_id)
    hypothesis = registry.get_hypothesis(
        engine, experiment.hypothesis_id, experiment.hypothesis_version
    )
    return hypothesis.family_id
