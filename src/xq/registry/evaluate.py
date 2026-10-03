"""`xq gate evaluate <bundle>`: the bundle's evidence against the gates (GATE-001).

The evaluator compiles what the registry holds about a bundle into one report and records the
gate results promotions read (MREG-002). It is wired to `xq validate-strategy`:

1. **The bundle** must load intact (`xq.registry.bundles.load_bundle`) and name its origin, the
   run and strategy it was built from.
2. **The validation.** A new validation of the origin strategy runs (`validate_run`, a run of
   kind ``validation``), unless an existing one is named; it must then be a finished validation
   of exactly that run and strategy. Its ``report.json`` is read, after checking it against the
   SHA-256 the registry recorded, and every check is rebuilt against ``config/gates.yaml``: a
   validation judged under other thresholds is refused, never reinterpreted.
3. **R1 and R2 gate results** are recorded from it (`record_gate_result` computes whether each
   passed; an incomplete verdict never passes). R3 is recorded by the vault evaluation (GATE-002),
   and R4 needs paper trading (Sprint 14).
4. **The report** (``reports/gates/<bundle>/<evaluation time>/gate.md`` and ``gate.json``) lists the
   plan's ten gate items (Phase 25) with their evidence and status, the reproduction status of
   the origin run (EXP-006, reported), the dataset's quality evidence, and links to every file it
   read. A pre-filled human review (``review.md``, GATE-003) is written next to it.

The evaluator never promotes: promotion is a separate, recorded decision (`xq registry promote`).
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine

import xq
from xq.core.config import AppConfig, GateCheck, GatesConfig, gates_hash
from xq.core.time import utc_now
from xq.data.raw_store import sha256_file
from xq.datasets.builder import NoDatasetDataError, read_manifest
from xq.registry.bundles import BundleRef, get_bundle, load_bundle
from xq.registry.gates import GateResultRef, latest_gate_result, record_gate_result
from xq.registry.models import RegistryStateError, SubjectKind
from xq.tracking import registry
from xq.tracking.registry import RunRef, RunStatus
from xq.validation.strategy import VALIDATION_KIND, validate_run

REPORT_DIR = "gates"
REVIEW_TEMPLATE = Path("docs/specs/gate-review.md")
EVALUATOR = f"xq {xq.__version__} gate evaluator"
#: The plan's release-gate items (Phase 25): number, name, evidence, and the criteria they read.
GATE_ITEMS: tuple[tuple[int, str, str, tuple[tuple[str, str], ...]], ...] = (
    (1, "Data validation", "quality runs of every partition used", ()),
    (
        2,
        "Baseline comparison",
        "baseline board, paired bootstrap",
        (("R1", "best_baseline_p_max"), ("R1", "best_baseline_margin_sharpe")),
    ),
    (
        3,
        "Statistical validation",
        "DSR, PBO, SPA report",
        (("R2", "dsr_min"), ("R2", "pbo_max"), ("R2", "spa_p_max")),
    ),
    (4, "Out-of-sample testing", "vault run record (R3)", ()),
    (
        5,
        "Walk-forward testing",
        "fold results",
        (("R2", "positive_folds_share_min"), ("R2", "decay_trend.significance")),
    ),
    (
        6,
        "Transaction-cost testing",
        "cost stress report",
        (("R2", "stressed_costs.net_sharpe_min"),),
    ),
    (
        7,
        "Robustness testing",
        "robustness score",
        (
            ("R2", "parameter_neighbourhood.profitable_share_min"),
            ("R2", "execution_delay.net_sharpe_min"),
        ),
    ),
    (8, "Monte Carlo testing", "Monte Carlo report", (("R2", "monte_carlo_drawdown.below"),)),
    (9, "Drawdown analysis", "drawdown statistics", (("R2", "oos_max_drawdown_max"),)),
    (10, "Paper trading", "paper ledger, parity report (R4)", ()),
)


class GateEvaluationError(RegistryStateError):
    """A bundle cannot be evaluated (no origin, a validation of something else, another policy)."""


@dataclass(frozen=True)
class GateItem:
    """One of the plan's release-gate items with its status."""

    number: int
    name: str
    evidence: str
    status: str
    detail: str


@dataclass(frozen=True)
class GateEvaluation:
    """The outcome of `evaluate_bundle`."""

    bundle: BundleRef
    validation_run_id: str
    results: dict[str, GateResultRef]
    items: tuple[GateItem, ...]
    report_dir: Path

    def summary_lines(self) -> list[str]:
        """The gate results and the items, one line each."""
        lines = [f"bundle {self.bundle.bundle_id} ({self.bundle.name}): {self.bundle.status}"]
        lines += [result.describe() for result in self.results.values()]
        lines += [f"{i.number:>2}. {i.name}: {i.status} ({i.detail})" for i in self.items]
        return lines


def evaluate_bundle(
    cfg: AppConfig,
    engine: Engine,
    bundle_id: str,
    *,
    validation_run_id: str | None = None,
    exploratory: bool = False,
) -> GateEvaluation:
    """Evaluate a bundle against R1 and R2 and write its gate report (module docstring).

    Args:
        validation_run_id: An existing validation of the bundle's origin strategy to read; by
            default a new one runs.
        exploratory: Allow a dirty git tree for the new validation (it is then not confirmatory).

    Raises:
        GateEvaluationError: for a bundle without an origin, a validation of another run or
            strategy, an unfinished validation, an altered report, or one judged under other
            thresholds.
        BundleIntegrityError: if the bundle does not load intact.
    """
    load_bundle(engine, bundle_id)
    bundle = get_bundle(engine, bundle_id)
    if bundle.origin_run_id is None or bundle.origin_strategy is None:
        raise GateEvaluationError(f"bundle {bundle.short_id} names no origin run and strategy")
    gates = cfg.gates_config()
    if validation_run_id is None:
        outcome = validate_run(
            cfg,
            engine,
            bundle.origin_run_id,
            strategy=bundle.origin_strategy,
            exploratory=exploratory,
        )
        validation = outcome.run
    else:
        validation = registry.get_run(engine, validation_run_id)
    _check_validation(validation, bundle)
    report_path, payload = _validation_report(engine, validation)
    when = utc_now()
    directory = (
        cfg.paths.resolve(cfg.paths.reports_dir)
        / REPORT_DIR
        / bundle.short_id
        / when.strftime("%Y%m%dT%H%M%S%fZ")
    )
    evidence = [str(report_path), str(report_path.with_name("report.md"))]
    evidence += [str(directory / "gate.md"), str(directory / "gate.json")]
    results: dict[str, GateResultRef] = {}
    for gate in ("R1", "R2"):
        checks = _checks(gates, payload, gate)
        results[gate] = record_gate_result(
            engine,
            gates,
            subject_kind=SubjectKind.BUNDLE,
            subject_id=bundle.bundle_id,
            gate=gate,
            checks=checks,
            not_evaluated=payload["not_evaluated"][gate],
            not_applicable=payload.get("not_applicable", {}).get(gate, {}),
            evaluator=EVALUATOR,
            evidence_paths=evidence,
            run_id=validation.run_id,
        )
    r3 = latest_gate_result(engine, SubjectKind.BUNDLE, bundle.bundle_id, "R3")
    if r3 is not None:
        results["R3"] = r3
    origin = registry.get_run(engine, bundle.origin_run_id)
    items = _items(results, payload, _quality(cfg, origin))
    bundle = get_bundle(engine, bundle.bundle_id)
    evaluation = GateEvaluation(bundle, validation.run_id, results, items, directory)
    _write(cfg, evaluation, gates, payload, _reproductions(engine, origin.run_id), when)
    return evaluation


def _check_validation(validation: RunRef, bundle: BundleRef) -> None:
    config = validation.config.get("run", {})
    if validation.kind != VALIDATION_KIND or config.get("validates") != bundle.origin_run_id:
        raise GateEvaluationError(
            f"run {validation.run_id} is not a validation of run {bundle.origin_run_id}"
        )
    if config.get("strategy") != bundle.origin_strategy:
        raise GateEvaluationError(
            f"run {validation.run_id} validated {config.get('strategy')!r}, not "
            f"{bundle.origin_strategy!r}"
        )
    if validation.status is not RunStatus.FINISHED:
        raise GateEvaluationError(f"validation run {validation.run_id} is {validation.status}")


def _validation_report(engine: Engine, validation: RunRef) -> tuple[Path, dict[str, Any]]:
    artifacts = [
        a for a in registry.list_artifacts(engine, validation.run_id) if a.kind == "validation_json"
    ]
    if len(artifacts) != 1:
        raise GateEvaluationError(f"validation run {validation.run_id} has no single JSON report")
    path = Path(artifacts[0].path)
    if not path.is_file() or sha256_file(path) != artifacts[0].sha256:
        raise GateEvaluationError(f"{path} is missing or altered since it was recorded")
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return path, payload


def _checks(gates: GatesConfig, payload: dict[str, Any], gate: str) -> list[GateCheck]:
    """The validation's checks of `gate`, rebuilt against the current evidence policy."""
    checks = []
    for row in payload["checks"]:
        if row["gate"] != gate:
            continue
        criterion = gates.criterion(gate, row["key"])
        if criterion.op != row["op"] or not math.isclose(criterion.threshold, row["threshold"]):
            raise GateEvaluationError(
                f"{gate} {row['key']} was judged at {row['op']} {row['threshold']}, but "
                f"config/gates.yaml now says {criterion.op} {criterion.threshold}: validate again"
            )
        value = math.nan if row["value"] is None else float(row["value"])
        check = dataclasses.replace(criterion, measure=row["measure"]).check(
            value, tuple(row["warnings"])
        )
        if check.passed != row["passed"]:
            raise GateEvaluationError(f"{gate} {row['key']}: the recorded outcome is inconsistent")
        checks.append(check)
    return checks


def _quality(cfg: AppConfig, origin: RunRef) -> dict[str, Any]:
    """The dataset's quality evidence (gate item 1) from its manifest."""
    if origin.dataset_id is None:
        return {"status": "not evaluated", "detail": "the origin run records no dataset"}
    try:
        manifest = read_manifest(cfg, origin.dataset_id)
    except NoDatasetDataError:
        return {"status": "not evaluated", "detail": f"dataset {origin.dataset_id} not found"}
    warned = manifest.get("warn_partitions", [])
    excluded = manifest.get("excluded_partitions", [])
    return {
        "status": "pass" if manifest.get("quality_run_ids") else "not evaluated",
        "detail": (
            f"dataset {origin.dataset_id} gated on quality run(s) "
            f"{', '.join(manifest.get('quality_run_ids', []))}: no FAIL partition included "
            f"({len(excluded)} excluded); {len(warned)} WARN partition(s) for the human review"
        ),
        "quality_run_ids": manifest.get("quality_run_ids", []),
        "warn_partitions": warned,
        "excluded_partitions": excluded,
    }


def _reproductions(engine: Engine, run_id: str) -> list[dict[str, Any]]:
    """The reproduction statuses of the origin run (EXP-006), oldest first."""
    statuses = []
    for run in registry.list_runs(engine):
        if run.kind != "reproduction" or run.config.get("run", {}).get("reproduces") != run_id:
            continue
        for artifact in registry.list_artifacts(engine, run.run_id):
            path = Path(artifact.path)
            if artifact.kind == "reproduction" and path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                statuses.append({"run_id": run.run_id, "status": data.get("status")})
    return statuses


def _items(
    results: dict[str, GateResultRef], payload: dict[str, Any], quality: dict[str, Any]
) -> tuple[GateItem, ...]:
    items = []
    for number, name, evidence, keys in GATE_ITEMS:
        if number == 1:
            items.append(GateItem(number, name, evidence, quality["status"], quality["detail"]))
            continue
        if number == 4:
            r3 = results.get("R3")
            status = "pending" if r3 is None else ("pass" if r3.passed else "fail")
            detail = "vault evaluation not run" if r3 is None else r3.describe()
            items.append(GateItem(number, name, evidence, status, detail))
            continue
        if number == 10:
            items.append(
                GateItem(number, name, evidence, "pending", "paper trading is not built (R4)")
            )
            continue
        items.append(GateItem(number, name, evidence, *_item_status(results, keys)))
    return tuple(items)


def _item_status(
    results: dict[str, GateResultRef], keys: Sequence[tuple[str, str]]
) -> tuple[str, str]:
    states, parts = [], []
    for gate, key in keys:
        values = results[gate].values
        if key in values["checks"]:
            check = values["checks"][key]
            states.append("pass" if check["passed"] else "fail")
            parts.append(f"{key} = {_fmt(check['value'])} {'pass' if check['passed'] else 'FAIL'}")
        elif key in values["not_applicable"]:
            states.append("not applicable")
            parts.append(f"{key}: not applicable ({values['not_applicable'][key]})")
        else:
            why = values["not_evaluated"].get(key, "missing")
            states.append("not evaluated")
            parts.append(f"{key}: not evaluated ({why})")
    if "fail" in states:
        status = "fail"
    elif "not evaluated" in states:
        status = "not evaluated"
    elif all(s == "not applicable" for s in states):
        status = "not applicable"
    else:
        status = "pass"
    return status, "; ".join(parts)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4g}"


def _write(
    cfg: AppConfig,
    evaluation: GateEvaluation,
    gates: GatesConfig,
    payload: dict[str, Any],
    reproductions: list[dict[str, Any]],
    when: pd.Timestamp,
) -> None:
    directory = evaluation.report_dir
    directory.mkdir(parents=True, exist_ok=True)
    bundle = evaluation.bundle
    reproduced = (
        "not attempted"
        if not reproductions
        else ", ".join(f"{r['status']} ({r['run_id']})" for r in reproductions)
    )
    document = {
        "bundle_id": bundle.bundle_id,
        "name": bundle.name,
        "status": str(bundle.status),
        "origin": {"run_id": bundle.origin_run_id, "strategy": bundle.origin_strategy},
        "evaluated_at": str(when),
        "evaluator": EVALUATOR,
        "gates_hash": gates_hash(gates),
        "validation_run_id": evaluation.validation_run_id,
        "synthetic": payload.get("synthetic", False),
        "cost_basis": payload.get("cost_basis"),
        "gate_results": {
            gate: {
                "gate_result_id": r.gate_result_id,
                "passed": r.passed,
                "values": r.values,
                "evidence_paths": r.evidence_paths,
            }
            for gate, r in evaluation.results.items()
        },
        "items": [dataclasses.asdict(i) for i in evaluation.items],
        "reproductions": reproductions,
    }
    (directory / "gate.json").write_text(
        json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    lines = [
        f"# Gate evaluation: {bundle.name} (`{bundle.short_id}`)",
        "",
        f"Bundle `{bundle.bundle_id}`, status **{bundle.status}**. Origin: run "
        f"`{bundle.origin_run_id}`, strategy `{bundle.origin_strategy}`. Evaluated {when} by "
        f"{EVALUATOR} under `config/gates.yaml` `{gates_hash(gates)}`. Net figures: "
        f"{payload.get('cost_basis')}.",
        "",
        "The evaluator records gate results; it never promotes. Promotion is a separate, recorded "
        "decision (`xq registry promote`), after the human review (GATE-003).",
        "",
        "## Gate results",
        "",
        *[f"- {r.describe()}" for r in evaluation.results.values()],
        "",
        "## Release-gate items (Phase 25)",
        "",
        "| # | Item | Evidence | Status | Detail |",
        "| --- | --- | --- | --- | --- |",
        *[
            f"| {i.number} | {i.name} | {i.evidence} | {i.status} | {i.detail} |"
            for i in evaluation.items
        ],
        "",
        f"Reproduction of the origin run (EXP-006, reported): {reproduced}.",
        "",
        "## Evidence",
        "",
        f"- Validation run `{evaluation.validation_run_id}`: "
        + ", ".join(f"`{p}`" for p in evaluation.results["R1"].evidence_paths[:2]),
        "",
    ]
    (directory / "gate.md").write_text("\n".join(lines), encoding="utf-8")
    _write_review(cfg, evaluation, directory, reproduced)


def _write_review(
    cfg: AppConfig, evaluation: GateEvaluation, directory: Path, reproduced: str
) -> None:
    """A copy of the human review template (GATE-003) with the evidence filled in."""
    candidates = [
        cfg.paths.resolve(cfg.paths.root) / REVIEW_TEMPLATE,
        Path(xq.__file__).resolve().parents[2] / REVIEW_TEMPLATE,  # a source checkout
    ]
    template = next((c for c in candidates if c.is_file()), None)
    if template is None:
        return
    bundle = evaluation.bundle
    text = template.read_text(encoding="utf-8")
    fields = {
        "{bundle_id}": bundle.bundle_id,
        "{bundle_name}": bundle.name,
        "{bundle_status}": str(bundle.status),
        "{origin}": f"{bundle.origin_run_id} / {bundle.origin_strategy}",
        "{gate_report}": str(directory / "gate.md"),
        "{validation_run}": evaluation.validation_run_id,
        "{gate_results}": "; ".join(r.describe() for r in evaluation.results.values()),
        "{reproduction}": reproduced,
    }
    for key, value in fields.items():
        text = text.replace(key, value)
    (directory / "review.md").write_text(text, encoding="utf-8")
