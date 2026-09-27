"""The significance report (Phase 17): its checks follow the gates' order, a criterion without
its inputs is not evaluated and makes the verdict incomplete, and every test becomes a stat_tests
row with the family's Holm adjustment."""

import dataclasses
import json

from helpers.quality import repo_config
from xq.robustness.simulated import SimulationSpec, simulated_subject
from xq.robustness.subject import family_trials
from xq.validation.report import SIGNIFICANCE_GATES, significance_report, verdict_of

CFG = repo_config()
GATES = CFG.gates_config()
FULL = CFG.validation_config()
SETTINGS = FULL.model_copy(
    update={"spa_size_check": FULL.spa_size_check.model_copy(update={"n_sim": 60})}
)


def subject(truth: str, seed: int = 0):  # type: ignore[no-untyped-def]
    made = simulated_subject(
        SimulationSpec(truth=truth, seed=seed),  # type: ignore[arg-type]
        capital=100_000.0,
        periods_per_year=252,
    )
    assert CFG.experiments is not None
    trials = family_trials(
        made.family,
        clustering=CFG.experiments.trial_clustering,
        gated=GATES.conventions.trial_count,
        periods_per_year=252,
    )
    return dataclasses.replace(made, trials=trials)


def test_the_checks_follow_the_gates_and_a_missing_baseline_is_not_evaluated() -> None:
    report = significance_report(
        dataclasses.replace(subject("genuine"), baselines=None),
        family_id="simulated",
        gates=GATES,
        settings=SETTINGS,
        seed=1,
    )
    keys = [(c.criterion.gate, c.criterion.key) for c in report.checks]
    expected = [
        (gate, key)
        for gate, names in SIGNIFICANCE_GATES.items()
        for key in names
        if not key.startswith("best_baseline")
    ]
    assert keys == expected
    assert set(report.gate_not_evaluated("R1")) == {
        "best_baseline_p_max",
        "best_baseline_margin_sharpe",
    }
    assert verdict_of(report.gate_checks("R1"), report.gate_not_evaluated("R1")) == "incomplete"
    assert verdict_of(report.gate_checks("R2"), report.gate_not_evaluated("R2")) == "pass"
    assert report.trials.gated == "effective"
    assert report.trials.n_effective < report.trials.n_raw  # correlated trend configurations


def test_every_test_is_a_json_safe_stat_tests_row() -> None:
    report = significance_report(
        subject("overfit"), family_id="noise", gates=GATES, settings=SETTINGS, seed=2
    )
    rows = report.stat_tests()
    names = [r.test_name for r in rows]
    assert names[:4] == ["sharpe_bootstrap", "deflated_sharpe", "min_track_record", "decay_trend"]
    assert {"pbo", "reality_check", "spa", "best_baseline"} <= set(names)
    assert sum(n.startswith("configuration:") for n in names) == 50
    assert all(r.family_id == "noise" for r in rows)
    json.dumps([dataclasses.asdict(r) for r in rows], allow_nan=False)
    assert verdict_of(report.gate_checks("R2"), report.gate_not_evaluated("R2")) == "fail"
    assert "Holm-adjusted" in report.markdown()
