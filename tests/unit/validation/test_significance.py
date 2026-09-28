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


def homogeneous(seed: int):  # type: ignore[no-untyped-def]
    """The genuine edge selected from near-identical configurations only (lookbacks 40-80)."""
    made = subject("genuine", seed)
    columns = [c for c in made.family.columns if float(c.split(",")[0].split("=")[1]) >= 40]
    family = made.family[columns]
    best = max(columns, key=lambda c: float(family[c].mean() / family[c].std()))
    assert CFG.experiments is not None
    trials = family_trials(
        family,
        clustering=CFG.experiments.trial_clustering,
        gated=GATES.conventions.trial_count,
        periods_per_year=252,
    )
    return dataclasses.replace(
        made, name=best, returns=family[best].rename("return"), family=family, trials=trials
    )


def test_pbo_does_not_apply_to_a_family_without_a_meaningful_selection() -> None:
    """C-25 (1): a genuine edge among near-identical configurations (at most two effective
    trials) is no longer failed by PBO, which only judges a choice there is none of here; the
    deflated Sharpe ratio still applies."""
    report = significance_report(
        homogeneous(0), family_id="simulated", gates=GATES, settings=SETTINGS, seed=3
    )
    assert report.trials.n_effective <= 2
    assert report.pbo is not None
    assert report.pbo.pbo > GATES.r2_validated.pbo_max  # judged, it would fail R2 on a coin toss
    keys = [c.criterion.key for c in report.gate_checks("R2")]
    assert "pbo_max" not in keys
    assert "dsr_min" in keys  # the DSR still applies
    assert report.gate_not_applicable("R2")["pbo_max"].startswith("no meaningful selection")
    assert "pbo_max" not in report.gate_not_evaluated("R2")
    assert verdict_of(report.gate_checks("R2"), report.gate_not_evaluated("R2")) == "pass"
    assert "PBO: not applicable: no meaningful selection" in report.markdown()
    (row,) = [t for t in report.stat_tests() if t.test_name == "pbo"]
    assert row.params["applicable"] is False


def test_pbo_still_fails_an_overfit_family_of_dispersed_configurations() -> None:
    report = significance_report(
        subject("overfit"), family_id="noise", gates=GATES, settings=SETTINGS, seed=4
    )
    assert report.trials.n_effective > SETTINGS.pbo.not_applicable_max_effective_trials
    assert report.not_applicable == {}
    (pbo,) = [c for c in report.gate_checks("R2") if c.criterion.key == "pbo_max"]
    assert not pbo.passed
    (row,) = [t for t in report.stat_tests() if t.test_name == "pbo"]
    assert row.params["applicable"] is True
