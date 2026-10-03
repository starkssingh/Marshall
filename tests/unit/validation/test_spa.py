"""VAL-004: White's Reality Check, Hansen's SPA and Romano-Wolf — about nominal size on noise-only
families (iid and with volatility clustering), a genuine edge detected with its survivors named,
SPA's power over the Reality Check when poor strategies dilute the family; under the gates' block
convention strong serial dependence in a short sample still over-rejects, and the per-sample size
check attaches its warning to the SPA gate result (C-24, ADR 0055); when it flags over-rejection
the gate reads the size-adjusted p-value from the sample's simulated null at the same threshold
(C-25, ADR 0058)."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from helpers.quality import repo_config
from helpers.simulate import ar1, garch_returns
from helpers.strategies import graded_family, noise_family
from xq.validation.spa import SizeCheck, family_tests, size_check

LEVEL = 0.10
CFG = repo_config()
GATES = CFG.gates_config()
BOOT = GATES.conventions.bootstrap
SIZE = CFG.validation_config().spa_size_check


def rejection_rates(make, replications: int) -> tuple[float, float, float]:  # type: ignore[no-untyped-def]
    rc = spa = stepm = 0
    for seed in range(replications):
        result = family_tests(make(seed), bootstrap=BOOT, n_boot=199, seed=10**6 + seed)
        rc += result.reality_check_p <= LEVEL
        spa += result.spa_p <= LEVEL
        stepm += bool((result.stepm_p <= LEVEL).any())
    return rc / replications, spa / replications, stepm / replications


def test_a_noise_only_family_is_rejected_at_about_the_nominal_rate() -> None:
    rates = rejection_rates(lambda seed: noise_family(250, 8, seed=seed), 400)
    # 400 replications: the Monte Carlo standard error at 10 % is 1.5 points
    for rate in rates:
        assert 0.05 <= rate <= 0.16


def test_volatility_clustering_keeps_the_size_near_nominal() -> None:
    def family(seed: int) -> np.ndarray:
        columns = [garch_returns(500, alpha=0.08, beta=0.9, seed=1000 * seed + k) for k in range(6)]
        return 0.01 * np.column_stack(columns)

    for rate in rejection_rates(family, 200):
        assert 0.04 <= rate <= 0.18


def test_a_genuine_edge_is_detected_and_its_survivors_named() -> None:
    frame = pd.DataFrame(
        graded_family(1000, 10, seed=1, top_sharpe=0.25), columns=[f"k{i}" for i in range(10)]
    )
    result = family_tests(frame, bootstrap=BOOT, n_boot=999, seed=1)
    assert result.reality_check_p < 0.01
    assert result.spa_p < 0.01
    survivors = result.survivors(LEVEL)
    assert "k9" in survivors  # the best configuration keeps its edge after the correction
    assert "k0" not in survivors  # a zero-mean configuration does not
    table = result.table()
    assert table.loc["k9", "romano_wolf_p"] <= LEVEL
    assert result.mean_block >= 5  # the gates' minimum block


def test_spa_keeps_its_power_when_poor_strategies_join_the_family() -> None:
    family = noise_family(500, 20, seed=3)
    family[:, 1:] -= 0.004  # nineteen clearly poor strategies ...
    family[:, 0] += 0.0012  # ... and one with a genuine edge
    result = family_tests(family, bootstrap=BOOT, n_boot=999, seed=3)
    assert result.spa_p < result.reality_check_p  # the Reality Check is dragged by the poor ones
    assert result.spa_p <= 0.01
    assert result.spa_p_lower <= result.spa_p <= result.spa_p_upper


def test_romano_wolf_is_monotone_and_matches_spa_for_one_strategy() -> None:
    result = family_tests(
        graded_family(600, 6, seed=4, top_sharpe=0.12), bootstrap=BOOT, n_boot=499, seed=4
    )
    order = np.argsort(-result.t_stats)
    assert np.all(np.diff(result.stepm_p[order]) >= 0)
    single = family_tests(
        graded_family(600, 2, seed=5, top_sharpe=0.2)[:, 1], bootstrap=BOOT, n_boot=499, seed=5
    )
    assert single.t_stats[0] > 0
    assert single.stepm_p[0] == pytest.approx(single.spa_p_upper)


def test_results_are_reproducible_and_inputs_checked() -> None:
    data = noise_family(200, 4, seed=7)
    first, second = (
        family_tests(data, bootstrap=BOOT, n_boot=99, seed=1),
        family_tests(data, bootstrap=BOOT, n_boot=99, seed=1),
    )
    assert first.spa_p == second.spa_p
    np.testing.assert_array_equal(first.stepm_p, second.stepm_p)
    bad = data.copy()
    bad[0, 0] = np.nan
    with pytest.raises(ValueError, match="missing"):
        family_tests(bad, bootstrap=BOOT, n_boot=99, seed=1)
    with pytest.raises(ValueError, match="two periods"):
        family_tests(data[:1], bootstrap=BOOT, n_boot=99, seed=1)


def ar1_family(periods: int, configs: int, phi: float, seed: int) -> np.ndarray:
    """A noise-only family whose strategies are each AR(1) with coefficient `phi`."""
    return 0.01 * np.column_stack([ar1(periods, phi, seed=1000 * seed + k) for k in range(configs)])


def test_strong_serial_dependence_still_over_rejects_under_the_gates_block_convention() -> None:
    # C-24 (1): the size simulation re-run with the gates' convention (Politis-White, at least 5
    # days) instead of any fixed block. 2,000 replications (ADR 0055): Reality Check 15.4 %,
    # SPA 18.5 %, Romano-Wolf 18.0 % at a 10 % level; here a 600-replication subset.
    rc, spa, stepm = rejection_rates(lambda seed: ar1_family(400, 8, 0.4, seed), 600)
    assert spa > 1.5 * LEVEL  # still above 1.5x nominal: gate results carry the warning
    assert stepm > 1.5 * LEVEL
    assert rc > 1.2 * LEVEL
    blocks = [
        family_tests(ar1_family(400, 8, 0.4, s), bootstrap=BOOT, n_boot=19, seed=s).mean_block
        for s in range(20)
    ]
    assert min(blocks) >= BOOT.min_block_days  # the convention's block, never a fixed one


def test_the_size_check_warns_on_a_dependent_short_sample_and_not_on_an_iid_one() -> None:
    settings = SIZE.model_copy(update={"n_sim": 300})
    dependent = ar1_family(400, 8, 0.4, seed=7)
    size = size_check(dependent, bootstrap=BOOT, settings=settings, level=LEVEL, seed=1)
    assert min(size.ar_orders) >= 1  # the sieve finds the dependence
    assert size.over_rejects("spa")
    result = family_tests(dependent, bootstrap=BOOT, n_boot=499, seed=1)
    check = result.gate_check(GATES, size)
    assert check.criterion.key == "spa_p_max"
    assert check.criterion.threshold == LEVEL  # the threshold never moves
    # C-25 (3): flagged, so the gate reads the size-adjusted p-value and names the raw one
    assert check.value == size.adjusted_p("spa", result.spa_p)
    assert "size-adjusted" in check.criterion.measure
    assert len(check.warnings) == 2
    assert check.warnings[0].startswith("test over-rejects on this sample")
    assert check.warnings[1] == (
        f"gated on the size-adjusted p-value; raw SPA p = {result.spa_p:.4g}"
    )
    assert "WARNING: test over-rejects on this sample" in check.describe()

    iid = noise_family(400, 8, seed=7)
    clean = size_check(iid, bootstrap=BOOT, settings=settings, level=LEVEL, seed=1)
    assert not clean.over_rejects("spa")
    assert not clean.over_rejects("reality_check")
    assert clean.warning("spa") is None
    clean_result = family_tests(iid, bootstrap=BOOT, n_boot=499, seed=1)
    clean_check = clean_result.gate_check(GATES, clean)
    assert clean_check.warnings == ()
    assert clean_check.value == clean_result.spa_p  # not flagged: the raw p-value is gated


def test_the_size_check_runs_at_the_gate_level_and_is_reproducible() -> None:
    settings = SIZE.model_copy(update={"n_sim": 60})
    data = noise_family(300, 3, seed=2)
    first = size_check(data, bootstrap=BOOT, settings=settings, level=LEVEL, seed=5)
    assert first == size_check(data, bootstrap=BOOT, settings=settings, level=LEVEL, seed=5)
    other = size_check(data, bootstrap=BOOT, settings=settings, level=0.05, seed=5)
    with pytest.raises(ValueError, match="gate's level"):
        family_tests(data, bootstrap=BOOT, n_boot=99, seed=1).gate_check(GATES, other)
    with pytest.raises(ValueError, match=r"\(0, 1\)"):
        size_check(data, bootstrap=BOOT, settings=settings, level=1.5, seed=5)


def test_the_size_adjusted_p_value_is_the_share_of_null_families_at_least_as_strong() -> None:
    null = (0.01, 0.04, 0.05, 0.05, 0.08, 0.2, 0.3, 0.5, 0.7, 0.9)
    size = SizeCheck(
        level=LEVEL,
        n_sim=len(null),
        reality_check_size=0.0,
        spa_size=sum(p <= LEVEL for p in null) / len(null),
        warn_ratio=1.5,
        ar_orders=(1,) * 3,
        reality_check_null_p=(0.5,) * len(null),
        spa_null_p=null,
    )
    assert size.over_rejects("spa")  # 5 of 10 null families reject at 10 %
    assert size.adjusted_p("spa", 0.05) == pytest.approx((1 + 4) / 11)  # ties count against it
    assert size.adjusted_p("spa", 0.001) == pytest.approx(1 / 11)  # never zero
    assert size.adjusted_p("spa", 1.0) == 1.0
    assert size.adjusted_p("reality_check", 0.05) == pytest.approx(1 / 11)
    values = [size.adjusted_p("spa", p) for p in np.linspace(0, 1, 41)]
    assert values == sorted(values)  # monotone in the raw p-value
    with pytest.raises(ValueError, match="no null p-values"):
        dataclasses.replace(size, spa_null_p=()).adjusted_p("spa", 0.05)


def test_a_raw_pass_that_the_sample_s_null_does_not_support_fails_the_gate() -> None:
    """On an over-rejecting sample a raw SPA p of 0.08 would pass the 0.10 threshold, but a
    fifth of the simulated null families are as strong: the size-adjusted p-value fails it."""
    null = tuple(np.linspace(0.0, 1.0, 101)[1:] ** 2)  # 31 % of null p-values below 0.10
    size = SizeCheck(
        level=LEVEL,
        n_sim=len(null),
        reality_check_size=0.1,
        spa_size=sum(p <= LEVEL for p in null) / len(null),
        warn_ratio=1.5,
        ar_orders=(1,),
        reality_check_null_p=null,
        spa_null_p=null,
    )
    result = dataclasses.replace(
        family_tests(noise_family(300, 3, seed=2), bootstrap=BOOT, n_boot=99, seed=1), spa_p=0.08
    )
    check = result.gate_check(GATES, size)
    assert check.criterion.threshold == LEVEL
    assert check.value == pytest.approx((1 + 28) / 101)  # 28 null p-values at most 0.08
    assert not check.passed
    assert "raw SPA p = 0.08" in check.describe()
