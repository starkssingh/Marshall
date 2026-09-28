"""The significance report and the combined validation report (Phase 17, `xq validate-strategy`).

`validate_strategy` judges one strategy (a `StrategySubject`) against the evidence gates of
``config/gates.yaml``. It combines a **significance report** (below) with the **robustness
report** (ROB-008, `xq.robustness.report`).

**Significance** (every test on the strategy's daily net returns, one-sided, under the gates'
bootstrap convention):

- R1 ``oos_net_sharpe_min``: the annualized net Sharpe ratio is positive.
- R1 ``oos_sharpe_p_max``: the stationary-bootstrap p-value of a positive Sharpe ratio (VAL-001)
  is below 0.05.
- R1 ``best_baseline_*``: the paired block bootstrap against the best baseline
  (`xq.validation.paired`), when the source has baselines.
- R1 ``min_oos_trades``: at least 100 closed trades.
- R2 ``dsr_min``: the deflated Sharpe ratio (VAL-002) with the family's gated trial count and the
  variance of its trials' Sharpe ratios.
- R2 ``pbo_max``: PBO by CSCV over the family's configuration matrix (VAL-003). A family with
  at most two effective trials (``pbo.not_applicable_max_effective_trials``) offers no meaningful
  selection: PBO judges the choice among configurations, and there is no choice to judge. PBO is
  then reported as "not applicable: no meaningful selection" and the criterion is not applicable;
  the deflated Sharpe ratio still applies (C-25, ADR 0057).
- R2 ``spa_p_max``: Hansen's SPA over the family against cash (VAL-004). It carries the size
  check's warning "test over-rejects on this sample" when the simulated size exceeds 1.5 times
  the level (ADR 0055), and the gate then reads the size-adjusted p-value from that sample's
  simulated null, at the same threshold (C-25, ADR 0057). Both p-values are reported, and the
  Reality Check's too. The Reality Check and the Romano-Wolf survivors are reported with it, and
  every configuration's bootstrap p-value is Holm-adjusted within the family (VAL-006).
- R2 ``decay_trend``: no significantly negative slope of the walk-forward folds' performance
  over time (`xq.validation.decay`).
- R2 ``min_track_record``: the evaluated days cover the minimum track record length at 95 %
  confidence.

**Verdicts.** Each of R1 and R2 is:

- ``pass`` when every one of its applicable criteria is evaluated and passes;
- ``fail`` when any fails;
- ``incomplete`` when none fails but one could not be evaluated, with the reason.

A criterion is **not applicable** only by a rule the owner decided (C-25, ADR 0057): PBO without a
meaningful selection, and the parameter neighbourhood of a strategy whose hypothesis declares its
parameters fixed a priori. It is listed with its reason and does not enter the verdict. A
criterion that merely could not be computed is never not applicable: it makes the verdict
incomplete.

R2's criteria are the significance ones above plus the seven robustness gates. Warnings never
change a verdict: thresholds are fixed. A synthetic subject's report says, first, that it is
never evidence.

**Records.** Every test becomes the plan's ``stat_tests`` row (`StatTest`), and every robustness
measure a ``robustness_results`` row, stored by the validation run (`xq.validation.strategy`).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from xq.core.config import GateCheck, GatesConfig, ValidationConfig
from xq.core.seeds import derive_seed
from xq.research.reports import markdown_table
from xq.risk.engine import RiskEngine
from xq.robustness.report import SYNTHETIC_BANNER, RobustnessReport, robustness_report
from xq.robustness.subject import StrategySubject, TrialSummary
from xq.validation.decay import DecayTrend, decay_trend
from xq.validation.dsr import DeflatedSharpe, deflated_sharpe_of_returns
from xq.validation.multiple_testing import adjust
from xq.validation.paired import BaselineTest, beats_best_baseline
from xq.validation.pbo import PBOResult, pbo_cscv
from xq.validation.sharpe import (
    SharpeBootstrap,
    SharpeEstimate,
    bootstrap_sharpe,
    estimate,
    gate_block_length,
    min_track_record_length,
)
from xq.validation.spa import FamilyTest, SizeCheck, family_tests, size_check

Gate = Literal["R1", "R2"]
Verdict = Literal["pass", "fail", "incomplete"]
#: The gate criteria the significance report judges, in report order.
SIGNIFICANCE_GATES: dict[str, tuple[str, ...]] = {
    "R1": (
        "oos_net_sharpe_min",
        "oos_sharpe_p_max",
        "best_baseline_p_max",
        "best_baseline_margin_sharpe",
        "min_oos_trades",
    ),
    "R2": (
        "dsr_min",
        "pbo_max",
        "spa_p_max",
        "decay_trend.significance",
        "min_track_record.confidence",
    ),
}


@dataclass(frozen=True)
class StatTest:
    """One statistical test: the plan's ``stat_tests`` row."""

    test_name: str
    family_id: str
    statistic: float | None
    p_value: float | None
    adjusted_p: float | None
    params: dict[str, Any]


def verdict_of(checks: tuple[GateCheck, ...], not_evaluated: dict[str, str]) -> Verdict:
    """``fail`` if any check fails, ``incomplete`` if one could not be evaluated, else ``pass``."""
    if any(not c.passed for c in checks):
        return "fail"
    return "incomplete" if not_evaluated else "pass"


@dataclass(frozen=True)
class SignificanceReport:
    """The statistical evidence on one strategy (module docstring)."""

    name: str
    family_id: str
    synthetic: bool
    periods_per_year: int
    n_days: int
    n_trades: int
    bootstrap: SharpeBootstrap
    estimate: SharpeEstimate
    min_track_record_days: float
    dsr: DeflatedSharpe
    trials: TrialSummary
    pbo: PBOResult | None
    family_test: FamilyTest
    size: SizeCheck
    #: Per configuration of the family: raw bootstrap p-value and its Holm adjustment.
    holm: pd.DataFrame
    decay: DecayTrend | None
    baseline: BaselineTest | None
    checks: tuple[GateCheck, ...]
    #: ``"R1 key"`` -> why that criterion could not be evaluated.
    not_evaluated: dict[str, str] = field(default_factory=dict)
    #: ``"R2 key"`` -> why that criterion does not apply (an owner's rule; module docstring).
    not_applicable: dict[str, str] = field(default_factory=dict)

    @property
    def sharpe(self) -> float:
        """Annualized net Sharpe ratio."""
        return self.estimate.sharpe * math.sqrt(self.periods_per_year)

    def gate_checks(self, gate: Gate) -> tuple[GateCheck, ...]:
        """The evaluated checks of `gate`."""
        return tuple(c for c in self.checks if c.criterion.gate == gate)

    def gate_not_evaluated(self, gate: Gate) -> dict[str, str]:
        """The criteria of `gate` that could not be evaluated, with the reason."""
        return _of_gate(self.not_evaluated, gate)

    def gate_not_applicable(self, gate: Gate) -> dict[str, str]:
        """The criteria of `gate` that do not apply, with the reason."""
        return _of_gate(self.not_applicable, gate)

    def stat_tests(self) -> list[StatTest]:
        """One row per test, for the ``stat_tests`` table."""
        family = self.family_id
        boot = self.bootstrap
        rows = [
            StatTest(
                "sharpe_bootstrap",
                family,
                _num(self.sharpe),
                _num(boot.p_value),
                None,
                {"n_boot": boot.n_boot, "mean_block": boot.mean_block, "one_sided": True},
            ),
            StatTest(
                "deflated_sharpe",
                family,
                _num(self.dsr.dsr),
                None,
                None,
                {
                    "n_trials": self.trials.n_gated,
                    "n_trials_raw": self.trials.n_raw,
                    "gated": self.trials.gated,
                    "benchmark_per_period": self.dsr.benchmark,
                },
            ),
            StatTest(
                "min_track_record",
                family,
                _num(self.n_days / self.min_track_record_days)
                if self.min_track_record_days > 0
                else None,
                None,
                None,
                {"min_track_record_days": _num(self.min_track_record_days), "days": self.n_days},
            ),
        ]
        if self.decay is not None:
            rows.append(
                StatTest(
                    "decay_trend",
                    family,
                    _num(self.decay.t_stat),
                    _num(self.decay.p_value),
                    None,
                    {"slope_per_year": self.decay.slope_per_year, "folds": self.decay.n_folds},
                )
            )
        if self.pbo is not None:
            rows.append(
                StatTest(
                    "pbo",
                    family,
                    _num(self.pbo.pbo),
                    None,
                    None,
                    {
                        "blocks": self.pbo.n_blocks,
                        "configurations": self.pbo.n_configurations,
                        "probability_of_loss": self.pbo.probability_of_loss,
                        "effective_trials": self.trials.n_effective,
                        "applicable": "R2 pbo_max" not in self.not_applicable,
                    },
                )
            )
        test = self.family_test
        size = {
            "n_sim": self.size.n_sim,
            "reality_check_size": self.size.reality_check_size,
            "spa_size": self.size.spa_size,
            "level": self.size.level,
        }
        rows.append(
            StatTest(
                "reality_check",
                family,
                _num(test.reality_check_stat),
                _num(test.reality_check_p),
                _num(self.size.adjusted_p("reality_check", test.reality_check_p)),
                {
                    "mean_block": test.mean_block,
                    "n_boot": test.n_boot,
                    "size_check": size,
                    "adjusted_p": "size-adjusted on the simulated null (reported, not gated)",
                },
            )
        )
        rows.append(
            StatTest(
                "spa",
                family,
                _num(test.spa_stat),
                _num(test.spa_p),
                _num(self.size.adjusted_p("spa", test.spa_p)),
                {
                    "lower": test.spa_p_lower,
                    "upper": test.spa_p_upper,
                    "mean_block": test.mean_block,
                    "n_boot": test.n_boot,
                    "size_check": size,
                    "adjusted_p": "size-adjusted on the simulated null",
                    "gate_reads": "size_adjusted" if self.size.over_rejects("spa") else "raw",
                },
            )
        )
        for strategy, row in self.holm.iterrows():
            rows.append(
                StatTest(
                    f"configuration:{strategy}",
                    family,
                    _num(row["t_stat"]),
                    _num(row["p"]),
                    _num(row["holm_p"]),
                    {"romano_wolf_p": _num(row["romano_wolf_p"]), "correction": "holm"},
                )
            )
        if self.baseline is not None:
            b = self.baseline
            rows.append(
                StatTest(
                    "best_baseline",
                    family,
                    _num(b.margin),
                    _num(b.p_value),
                    None,
                    {"baseline": b.baseline, "mean_block": b.mean_block, "n_boot": b.n_boot},
                )
            )
        return rows

    def markdown(self) -> str:
        """The significance section of the validation report."""
        out = ["## Significance (VAL-001 ... VAL-006)", ""]
        if self.synthetic:
            out += [SYNTHETIC_BANNER, ""]
        est = self.estimate
        root = math.sqrt(self.periods_per_year)
        out += [
            f"- Annualized net Sharpe ratio {self.sharpe:.3f} over {self.n_days} days and "
            f"{self.n_trades} closed trades; bootstrap interval "
            f"[{self.bootstrap.ci_low * root:.3f}, {self.bootstrap.ci_high * root:.3f}], "
            f"one-sided p = {self.bootstrap.p_value:.4g}.",
            f"- Standard errors (annualized): iid {est.se_iid * root:.3f}, non-normal "
            f"{est.se_non_normal * root:.3f}, HAC {est.se_hac * root:.3f}.",
            f"- Minimum track record length at 95 %: {self.min_track_record_days:.0f} days.",
            f"- Deflated Sharpe ratio {self.dsr.dsr:.4f} with {self.trials.n_gated:g} "
            f"{self.trials.gated} trials ({self.trials.n_raw} raw).",
            self._pbo_line(),
            f"- Family of {len(self.holm)}: Reality Check p = "
            f"{self.family_test.reality_check_p:.4g}, SPA p = {self.family_test.spa_p:.4g} "
            f"(lower {self.family_test.spa_p_lower:.4g}, "
            f"upper {self.family_test.spa_p_upper:.4g}); "
            f"simulated size at {self.size.level:.0%}: SPA {self.size.spa_size:.1%}, Reality "
            f"Check {self.size.reality_check_size:.1%} ({self.size.n_sim} null families).",
            self._size_adjusted_line(),
        ]
        if self.decay is not None:
            out.append(
                f"- Decay over {self.decay.n_folds} folds: slope {self.decay.slope_per_year:.3g} "
                f"a year, one-sided p = {self.decay.p_value:.4g}."
            )
        warning = self.size.warning("reality_check")
        if warning is not None:
            out.append(f"- WARNING (Reality Check, reported): {warning}.")
        if self.baseline is not None:
            b = self.baseline
            out.append(
                f"- Best baseline {b.baseline}: Sharpe {b.baseline_sharpe:.3f}, margin "
                f"{b.margin:.3f}, paired bootstrap p = {b.p_value:.4g}."
            )
        out += ["", "Configurations of the family, Holm-adjusted within it (VAL-006):", ""]
        out += [markdown_table(self.holm.reset_index())]
        return "\n".join(out)

    def _size_adjusted_line(self) -> str:
        test, size = self.family_test, self.size
        reads = "size-adjusted" if size.over_rejects("spa") else "raw"
        why = (
            "the size check flags over-rejection"
            if size.over_rejects("spa")
            else f"the simulated size is within {size.warn_ratio:g}x the level"
        )
        return (
            f"- Size-adjusted p-values (share of the {size.n_sim} simulated null families at "
            f"least as strong): SPA {size.adjusted_p('spa', test.spa_p):.4g}, Reality Check "
            f"{size.adjusted_p('reality_check', test.reality_check_p):.4g}. The SPA gate reads "
            f"the {reads} p-value: {why}."
        )

    def _pbo_line(self) -> str:
        why = self.not_applicable.get("R2 pbo_max")
        if why is not None:
            shown = (
                ""
                if self.pbo is None
                else f"; the CSCV value {self.pbo.pbo:.3f} is shown for reference, not judged"
            )
            return f"- PBO: not applicable: {why}{shown}."
        if self.pbo is None:
            return "- PBO: not evaluated."
        return (
            f"- PBO {self.pbo.pbo:.3f} ({self.pbo.n_configurations} configurations, "
            f"{self.trials.n_effective:g} effective); probability of loss "
            f"{self.pbo.probability_of_loss:.3f}."
        )


def significance_report(
    subject: StrategySubject,
    *,
    family_id: str,
    gates: GatesConfig,
    settings: ValidationConfig,
    seed: int,
) -> SignificanceReport:
    """The significance tests of `subject` against `gates` (module docstring)."""
    periods = subject.periods_per_year
    r = subject.returns.to_numpy(np.float64)
    convention = gates.conventions.bootstrap
    checks: dict[str, GateCheck] = {}
    not_evaluated: dict[str, str] = {}
    not_applicable: dict[str, str] = {}

    est = estimate(r)
    block = gate_block_length(r, convention.block_length, convention.min_block_days)
    boot = bootstrap_sharpe(
        r, n_boot=convention.n_boot, mean_block=block, seed=derive_seed(seed, "sharpe")
    )
    annual = est.sharpe * math.sqrt(periods)
    checks["R1 oos_net_sharpe_min"] = gates.criterion("R1", "oos_net_sharpe_min").check(annual)
    checks["R1 oos_sharpe_p_max"] = gates.criterion("R1", "oos_sharpe_p_max").check(boot.p_value)

    baseline = None
    if subject.baselines is not None and subject.baselines.shape[1]:
        baseline = beats_best_baseline(
            subject.returns,
            subject.baselines,
            bootstrap=convention,
            periods_per_year=periods,
            seed=derive_seed(seed, "best_baseline"),
        )
        p_check, margin_check = baseline.gate_checks(gates)
        checks["R1 best_baseline_p_max"] = p_check
        checks["R1 best_baseline_margin_sharpe"] = margin_check
    else:
        for key in ("best_baseline_p_max", "best_baseline_margin_sharpe"):
            not_evaluated[f"R1 {key}"] = "the source has no baselines on the same days"
    n_trades = len(subject.trades)
    checks["R1 min_oos_trades"] = gates.criterion("R1", "min_oos_trades").check(float(n_trades))

    trials = subject.trials
    dsr = deflated_sharpe_of_returns(
        r, n_trials=trials.n_gated, sharpe_variance=trials.sharpe_variance / periods
    )
    checks["R2 dsr_min"] = gates.criterion("R2", "dsr_min").check(dsr.dsr)

    family = subject.family
    blocks = settings.pbo.blocks
    pbo = None
    if family.shape[1] >= 2 and len(family) >= 2 * blocks:
        pbo = pbo_cscv(family.to_numpy(np.float64), n_blocks=blocks)
    effective = trials.n_effective
    limit = settings.pbo.not_applicable_max_effective_trials
    if effective <= limit:
        not_applicable["R2 pbo_max"] = (
            f"no meaningful selection (the family has {effective:g} effective "
            f"trial{'' if effective == 1 else 's'}, at most {limit:g})"
        )
    elif pbo is not None:
        checks["R2 pbo_max"] = gates.criterion("R2", "pbo_max").check(pbo.pbo)
    else:
        not_evaluated["R2 pbo_max"] = (
            f"PBO needs at least two configurations over {2 * blocks} days"
        )

    test = family_tests(family, bootstrap=convention, seed=derive_seed(seed, "spa"))
    size = size_check(
        family,
        bootstrap=convention,
        settings=settings.spa_size_check,
        level=gates.r2_validated.spa_p_max,
        seed=derive_seed(seed, "spa_size_check"),
    )
    checks["R2 spa_p_max"] = test.gate_check(gates, size)
    holm = test.table().assign(holm_p=adjust(test.single_p, "holm"))

    decay = None
    try:
        decay = decay_trend(r, subject.folds.to_numpy(), periods_per_year=periods)
        checks["R2 decay_trend.significance"] = decay.gate_check(gates)
    except ValueError as exc:
        not_evaluated["R2 decay_trend.significance"] = str(exc)

    confidence = gates.r2_validated.min_track_record.confidence
    trl = (
        min_track_record_length(est.sharpe, est.skew, est.kurtosis, alpha=1 - confidence)
        if math.isfinite(est.sharpe) and math.isfinite(est.skew)
        else math.inf
    )
    checks["R2 min_track_record.confidence"] = gates.criterion(
        "R2", "min_track_record.confidence"
    ).check(len(r) / trl if trl > 0 else math.nan)

    order = [f"{g} {k}" for g, keys in SIGNIFICANCE_GATES.items() for k in keys]
    return SignificanceReport(
        name=subject.name,
        family_id=family_id,
        synthetic=subject.synthetic,
        periods_per_year=periods,
        n_days=len(r),
        n_trades=n_trades,
        bootstrap=boot,
        estimate=est,
        min_track_record_days=trl,
        dsr=dsr,
        trials=trials,
        pbo=pbo,
        family_test=test,
        size=size,
        holm=holm,
        decay=decay,
        baseline=baseline,
        checks=tuple(checks[k] for k in order if k in checks),
        not_evaluated=not_evaluated,
        not_applicable=not_applicable,
    )


@dataclass(frozen=True)
class StrategyValidation:
    """The combined significance and robustness report of one strategy (module docstring)."""

    name: str
    source: str
    synthetic: bool
    cost_basis: str
    family_id: str
    significance: SignificanceReport
    robustness: RobustnessReport

    def checks(self, gate: Gate) -> tuple[GateCheck, ...]:
        """Every evaluated check of `gate` (R2: significance, then robustness)."""
        own = self.significance.gate_checks(gate)
        return own + self.robustness.checks if gate == "R2" else own

    def not_evaluated(self, gate: Gate) -> dict[str, str]:
        """Every criterion of `gate` that could not be evaluated."""
        missing = self.significance.gate_not_evaluated(gate)
        if gate == "R2":
            missing.update(self.robustness.not_evaluated)
        return missing

    def not_applicable(self, gate: Gate) -> dict[str, str]:
        """Every criterion of `gate` that does not apply, with the reason (module docstring)."""
        missing = self.significance.gate_not_applicable(gate)
        if gate == "R2":
            missing.update(self.robustness.not_applicable)
        return missing

    def verdict(self, gate: Gate) -> Verdict:
        """``pass``, ``fail`` or ``incomplete`` for `gate` (module docstring)."""
        return verdict_of(self.checks(gate), self.not_evaluated(gate))

    @property
    def warnings(self) -> list[str]:
        """Every warning attached to an evaluated check."""
        return [w for gate in ("R1", "R2") for c in self.checks(gate) for w in c.warnings]

    def summary_lines(self) -> list[str]:
        """The verdicts, every gate line and the robustness score."""
        lines = []
        if self.synthetic:
            lines.append(
                "SYNTHETIC: an engineering check of the validation machinery, not evidence"
            )
        lines.append(f"strategy {self.name} ({self.source}); net figures: {self.cost_basis}")
        for gate in ("R1", "R2"):
            lines.append(f"{gate}: {self.verdict(gate).upper()}")
            lines.extend(f"  {c.describe()}" for c in self.checks(gate))
            lines.extend(
                f"  {gate} {key}: not evaluated: {why}"
                for key, why in self.not_evaluated(gate).items()
            )
            lines.extend(
                f"  {gate} {key}: not applicable: {why}"
                for key, why in self.not_applicable(gate).items()
            )
        lines.append(self.robustness.summary_lines()[0])
        return lines

    def markdown(self) -> str:
        """The whole report as Markdown."""
        out = [f"# Strategy validation: {self.name}", ""]
        if self.synthetic:
            out += [SYNTHETIC_BANNER, ""]
        out += [
            f"Source: {self.source}. Test family: `{self.family_id}`. Net figures: "
            f"{self.cost_basis}. Gates: `config/gates.yaml` (thresholds fixed before results).",
            "",
            "## Verdicts",
            "",
        ]
        out += ["```", *self.summary_lines(), "```", ""]
        out += [self.significance.markdown(), "", self.robustness.markdown()]
        return "\n".join(out)

    def to_json(self) -> dict[str, Any]:
        """Verdicts, every check and every stat_tests / robustness_results row, JSON-safe."""

        def check(c: GateCheck) -> dict[str, Any]:
            return {
                "gate": c.criterion.gate,
                "key": c.criterion.key,
                "measure": c.criterion.measure,
                "op": c.criterion.op,
                "threshold": c.criterion.threshold,
                "value": _num(c.value),
                "passed": c.passed,
                "warnings": list(c.warnings),
            }

        return {
            "strategy": self.name,
            "source": self.source,
            "synthetic": self.synthetic,
            "cost_basis": self.cost_basis,
            "family_id": self.family_id,
            "verdicts": {g: self.verdict(g) for g in ("R1", "R2")},
            "checks": [check(c) for g in ("R1", "R2") for c in self.checks(g)],
            "not_evaluated": {g: self.not_evaluated(g) for g in ("R1", "R2")},
            "not_applicable": {g: self.not_applicable(g) for g in ("R1", "R2")},
            "robustness_score": _num(self.robustness.score),
            "robustness_verdict": self.robustness.verdict,
            "stat_tests": [asdict(t) for t in self.significance.stat_tests()],
            "robustness_results": [asdict(r) for r in self.robustness.results()],
        }


def validate_strategy(
    subject: StrategySubject,
    *,
    family_id: str,
    gates: GatesConfig,
    settings: ValidationConfig,
    risk_engine: RiskEngine,
    seed: int,
) -> StrategyValidation:
    """The significance and robustness reports of `subject` (module docstring)."""
    significance = significance_report(
        subject, family_id=family_id, gates=gates, settings=settings, seed=seed
    )
    robustness = robustness_report(
        subject, gates=gates, settings=settings, risk_engine=risk_engine, seed=seed
    )
    return StrategyValidation(
        name=subject.name,
        source=subject.source,
        synthetic=subject.synthetic,
        cost_basis=subject.cost_basis,
        family_id=family_id,
        significance=significance,
        robustness=robustness,
    )


def _of_gate(entries: dict[str, str], gate: Gate) -> dict[str, str]:
    prefix = f"{gate} "
    return {key.removeprefix(prefix): why for key, why in entries.items() if key.startswith(prefix)}


def _num(value: Any) -> float | None:
    """A JSON-safe number: NaN and infinities become None."""
    number = float(value)
    return number if math.isfinite(number) else None
