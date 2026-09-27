"""The statistical verdict report (STAT-008): Observed / Evidence / Interpretation / Limitations /
Action per method.

Each builder turns a typed result of STAT-001, STAT-002, STAT-003 or STAT-006 into a
`MethodVerdict`: what was observed (the numbers), the evidence (tests, levels, multiplicity
corrections and the recovery tests the method passed on simulated processes), the interpretation,
its limitations, and the action it leads to. The wording follows the plan's method table: a
non-rejected unit root is not proof of one, ARCH effects are not return predictability, a
variance ratio counts only when the joint test rejects, and in-sample significance is recorded but
never promoted — only out-of-sample Diebold-Mariano evidence after Holm is **useful evidence**.

`build_verdict_report` writes the verdicts as a deterministic report (`xq.research.reports`):
``verdict.md`` with a summary table and one block per method, plus the raw result tables. Sprint 6
is build-only: the report is exercised on simulated results only, never on real data (ADR 0043).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from xq.research.recovery import recovery_tests
from xq.research.reports import ReportBuilder
from xq.research.stats.arima import ArmaStudy
from xq.research.stats.dependence import DependenceTests
from xq.research.stats.stationarity import StationarityBattery
from xq.research.stats.variance_ratio import VarianceRatioTests

Status = Literal["useful evidence", "evidence", "no evidence", "inconclusive"]
SECTION = "verdict"
FIELDS = ("observed", "evidence", "interpretation", "limitations", "action")


@dataclass(frozen=True)
class MethodVerdict:
    """The verdict on one method applied to one series."""

    task: str
    method: str
    question: str
    series: str
    status: Status
    observed: str
    evidence: str
    interpretation: str
    limitations: str
    action: str
    recovery: tuple[str, ...]
    table: pd.DataFrame | None = None

    def __post_init__(self) -> None:
        empty = [name for name in FIELDS if not getattr(self, name).strip()]
        if empty:
            raise ValueError(f"a verdict needs every field; empty: {empty}")
        if not self.recovery:
            raise ValueError(f"{self.method} cites no recovery test; it must not be used")

    def markdown(self) -> str:
        """The verdict block in the Observed / Evidence / ... / Action format."""
        lines = [
            f"## {self.task} {self.method} — {self.series}",
            "",
            f"**Question:** {self.question}",
            "",
            f"**Status:** {self.status}",
            "",
            f"- **Observed:** {self.observed}",
            f"- **Evidence:** {self.evidence}",
            f"- **Interpretation:** {self.interpretation}",
            f"- **Limitations:** {self.limitations}",
            f"- **Action:** {self.action}",
            "",
            "Recovery tests passed on simulated processes: "
            + ", ".join(f"`{node.split('::')[-1]}`" for node in self.recovery),
        ]
        return "\n".join(lines)


def _recovery(*methods: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(node for m in methods for node in recovery_tests(m)))


def stationarity_verdict(battery: StationarityBattery) -> MethodVerdict:
    """STAT-001: the joint reading of ADF, PP, KPSS and Zivot-Andrews on one series."""
    adf, pp, kpss = battery.result("ADF"), battery.result("PP"), battery.result("KPSS(c)")
    za = battery.result("ZA")
    observed = (
        f"ADF {adf.statistic:.3f} (p {adf.p_value:.3g}, {adf.lags} lags), PP {pp.statistic:.3f} "
        f"(p {pp.p_value:.3g}), KPSS level {kpss.statistic:.3f} (p {kpss.p_value:.3g}), "
        f"Zivot-Andrews {za.statistic:.3f} (p {za.p_value:.3g}, break at "
        f"{za.details.get('break_date')}); n = {adf.nobs}."
    )
    evidence = f"Joint verdict **{battery.verdict}** at level {adf.alpha:g}: {battery.explanation}."
    interpretation, action = {
        "stationary": (
            "The series behaves as stationary around a level.",
            "It may be modelled in levels; no differencing is needed for it.",
        ),
        "unit_root": (
            "The series behaves as integrated of order one; its level wanders.",
            "Model its differences (returns), not its level; level-based features need a "
            "stationarity-preserving transform (STAT-005, a candidate only).",
        ),
        "conflicting": (
            "Both hypotheses are rejected: long memory, a break or near-unit-root dynamics.",
            "Check long memory (STAT-004) and subsample stability before any level-based "
            "feature; do not choose a transform from this result alone.",
        ),
        "inconclusive": (
            "The tests cannot tell a unit root from stationarity on this sample.",
            "No transform decision from this test; prefer returns, which need no such choice.",
        ),
        "mixed": (
            "ADF and Phillips-Perron disagree, so neither reading is supported.",
            "No transform decision from this test; report the disagreement.",
        ),
    }[battery.verdict]
    return MethodVerdict(
        task="STAT-001",
        method="stationarity battery",
        question="Does the series have a unit root, or is it stationary?",
        series=battery.series,
        status="inconclusive" if battery.verdict in ("inconclusive", "mixed") else "evidence",
        observed=observed,
        evidence=evidence,
        interpretation=interpretation,
        limitations=(
            "Non-rejection is not proof of a unit root; the tests have low power near one. "
            "One break is searched once (Zivot-Andrews), not many; KPSS p-values are "
            "interpolated in a table. Descriptive of the discovery window only."
        ),
        action=action,
        recovery=_recovery("ADF", "PP", "KPSS", "ZA"),
        table=battery.table(),
    )


def dependence_verdict(tests: DependenceTests) -> MethodVerdict:
    """STAT-002: return autocorrelation (robust Q*) and volatility clustering (ARCH-LM, LB)."""
    robust = tests.any_rejects("Q*", "returns")
    plain = tests.any_rejects("LB", "returns")
    clustering = tests.any_rejects("ARCH-LM") or tests.any_rejects("LB", "squared_returns")
    q_star = tests.select("Q*", "returns")
    arch = tests.select("ARCH-LM")
    observed = (
        "Robust Q* on returns: "
        + ", ".join(f"lag {r.lags} p_holm {r.details['p_holm']:.3g}" for r in q_star)
        + "; ARCH-LM: "
        + ", ".join(
            f"lag {r.lags} LM {r.statistic:.1f} p_holm {r.details['p_holm']:.3g}" for r in arch
        )
        + "."
    )
    evidence = (
        f"Holm across lags within each family at level {q_star[0].alpha:g}: robust return "
        f"autocorrelation {'rejects' if robust else 'does not reject'}; plain Ljung-Box on "
        f"returns {'rejects' if plain else 'does not reject'}; volatility clustering "
        f"{'rejects' if clustering else 'does not reject'} no-ARCH."
    )
    if robust:
        interpretation = (
            "Returns show linear serial dependence beyond what volatility clustering explains."
        )
        action = (
            "Evaluate AR/ARMA forecasts out of sample (STAT-006); an in-sample rejection is not "
            "an edge."
        )
        status: Status = "evidence"
    else:
        interpretation = "No evidence of linear return dependence once heteroskedasticity is " + (
            "allowed for (the plain Ljung-Box rejection reflects volatility clustering)."
            if plain
            else "allowed for."
        )
        action = "Do not expect AR models to help; they stay benchmarks (BASE-003)."
        status = "no evidence"
    if clustering:
        interpretation += " Volatility clusters, as is typical of financial returns."
        action += " Proceed with volatility models (VOL-003, VOL-004); clustering is not "
        action += "return predictability."
    return MethodVerdict(
        task="STAT-002",
        method="dependence tests",
        question="Are returns serially correlated, and does volatility cluster?",
        series=tests.series,
        status=status,
        observed=observed,
        evidence=evidence,
        interpretation=interpretation,
        limitations=(
            "Linear dependence only; the plain Ljung-Box over-rejects under clustering, so only "
            "Q* speaks to returns. Lags were fixed in advance; the discovery window only."
        ),
        action=action,
        recovery=_recovery("LB", "Q*", "ARCH-LM"),
        table=tests.table(),
    )


def variance_ratio_verdict(
    tests: VarianceRatioTests, slices: pd.DataFrame | None = None
) -> MethodVerdict:
    """STAT-003: trend or reversion at 2-64 bars, read through the Chow-Denning joint test."""
    joint = tests.joint
    ratios = {r.lags: r.details["variance_ratio"] for r in tests.horizons}
    strongest = max(
        tests.horizons, key=lambda r: abs(r.statistic) if math.isfinite(r.statistic) else -1.0
    )
    observed = (
        "VR(q): "
        + ", ".join(f"{q}: {v:.3f}" for q, v in ratios.items())
        + f"; largest |z*| {joint.statistic:.2f} at q = {strongest.lags}."
    )
    evidence = (
        f"Chow-Denning joint p {joint.p_value:.3g} over {joint.details['horizons']} horizons "
        f"at level {joint.alpha:g} ({'rejects' if joint.reject else 'does not reject'})."
    )
    status: Status
    if joint.reject:
        direction = "reversion" if strongest.details["variance_ratio"] < 1 else "persistence"
        interpretation = f"Returns show {direction} at about {strongest.lags} bars."
        action = (
            f"A {direction} hypothesis at that horizon may be pre-registered and tested on later "
            "data; it is not a trading result."
        )
        status = "evidence"
    else:
        interpretation = "No evidence of trend or reversion at 2 to 64 bars."
        action = "Do not build momentum or reversion hypotheses on these horizons from this test."
        status = "no evidence"
    if slices is not None and not slices.empty and "p_holm_slices" in slices:
        rejected = slices.loc[slices["p_holm_slices"] < joint.alpha, "slice"].tolist()
        observed += f" Slices rejecting after Holm: {', '.join(rejected) or 'none'}."
    return MethodVerdict(
        task="STAT-003",
        method="variance ratios",
        question="Do returns trend or revert at horizons of 2 to 64 bars?",
        series=tests.series,
        status=status,
        observed=observed,
        evidence=evidence,
        interpretation=interpretation,
        limitations=(
            "Overlapping horizons are dependent; the joint bound is conservative. Slices are "
            "descriptive unless pre-registered; stability out of sample is not shown here."
        ),
        action=action,
        recovery=_recovery("VR", "Chow-Denning"),
        table=tests.table() if slices is None or slices.empty else slices,
    )


def arma_verdict(study: ArmaStudy, series: str) -> MethodVerdict:
    """STAT-006: out-of-sample linear forecastability against the random walk."""
    table = study.comparisons
    models = list(dict.fromkeys(table["model"])) if not table.empty else []
    useful = [m for m in models if study.useful(m)]
    lines = []
    for row in table.to_dict("records"):
        lines.append(
            f"{row['model']} vs {row['benchmark']} at {row['horizon']}: MSE change "
            f"{-float(row['mse_reduction']):+.2%}, p_holm {float(row['p_holm']):.3g}"
        )
    in_sample = ", ".join(
        f"{name}: " + ", ".join(f"{k} {v:.3g}" for k, v in fit.params().items())
        for name, fit in study.in_sample.items()
    )
    observed = "; ".join(lines) + "." if lines else "No comparison could be made."
    evidence = (
        "Diebold-Mariano on squared errors, one-sided, Holm across horizons for each model and "
        "benchmark, identical walk-forward folds. In-sample coefficients (first training fold, "
        f"recorded, not evidence): {in_sample or 'none'}."
    )
    if useful:
        status: Status = "useful evidence"
        interpretation = (
            f"{', '.join(useful)} forecast better than every benchmark out of sample at some "
            "horizon."
        )
        action = (
            "Compare the gain with the horizon's cost-to-volatility ratio (EDA-006); only if it "
            "is large relative to costs, pre-register a hypothesis. The AR stays on the board "
            "(BASE-003)."
        )
    else:
        status = "no evidence"
        interpretation = "No linear model beats the random walk out of sample after Holm."
        action = "Keep AR/ARMA as board baselines only (BASE-003); do not pursue linear models."
    return MethodVerdict(
        task="STAT-006",
        method="ARMA forecasts",
        question="Do linear models forecast returns better than the random walk out of sample?",
        series=series,
        status=status,
        observed=observed,
        evidence=evidence,
        interpretation=interpretation,
        limitations=(
            "Squared-error gains ignore costs; the folds are the walk-forward test folds only "
            "and the vault is untouched. Each (model, horizon) counts as a trial."
        ),
        action=action,
        recovery=_recovery("ARMA", "DM"),
        table=table,
    )


def build_verdict_report(
    verdicts: Sequence[MethodVerdict],
    directory: Path,
    *,
    metadata: Mapping[str, Any],
    title: str = "Statistical verdict report",
) -> dict[str, str]:
    """Write the verdict report into `directory` (module docstring); return the file digests."""
    if not verdicts:
        raise ValueError("a verdict report needs at least one verdict")
    builder = ReportBuilder(title, metadata=metadata)
    section = builder.section(SECTION, title)
    summary = pd.DataFrame(
        [
            {
                "task": v.task,
                "method": v.method,
                "series": v.series,
                "status": v.status,
                "action": v.action,
            }
            for v in verdicts
        ]
    )
    section.text(
        "Every method below passed its recovery test on a simulated process before use. "
        "Only out-of-sample evidence after multiplicity correction is **useful evidence**; "
        "in-sample significance is recorded, never promoted."
    )
    section.table("summary", summary, caption="Verdicts")
    for number, verdict in enumerate(verdicts, start=1):
        section.text(verdict.markdown())
        if verdict.table is not None and not verdict.table.empty:
            section.table(
                f"{verdict.task.lower()}-{number}",
                verdict.table,
                caption=f"{verdict.task} {verdict.method} — {verdict.series}: results",
                max_rows=0,
            )
    return builder.build(directory)
