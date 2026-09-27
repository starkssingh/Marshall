"""VAL-004: White's Reality Check, Hansen's SPA and Romano-Wolf — about nominal size on noise-only
families (iid and with volatility clustering), a genuine edge detected with its survivors named,
SPA's power over the Reality Check when poor strategies dilute the family."""

import numpy as np
import pandas as pd
import pytest

from helpers.simulate import garch_returns
from helpers.strategies import graded_family, noise_family
from xq.validation.spa import family_tests

LEVEL = 0.10


def rejection_rates(make, replications: int) -> tuple[float, float, float]:  # type: ignore[no-untyped-def]
    rc = spa = stepm = 0
    for seed in range(replications):
        result = family_tests(make(seed), n_boot=199, seed=10**6 + seed)
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
    result = family_tests(frame, n_boot=999, seed=1)
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
    result = family_tests(family, n_boot=999, seed=3)
    assert result.spa_p < result.reality_check_p  # the Reality Check is dragged by the poor ones
    assert result.spa_p <= 0.01
    assert result.spa_p_lower <= result.spa_p <= result.spa_p_upper


def test_romano_wolf_is_monotone_and_matches_spa_for_one_strategy() -> None:
    result = family_tests(graded_family(600, 6, seed=4, top_sharpe=0.12), n_boot=499, seed=4)
    order = np.argsort(-result.t_stats)
    assert np.all(np.diff(result.stepm_p[order]) >= 0)
    single = family_tests(graded_family(600, 2, seed=5, top_sharpe=0.2)[:, 1], n_boot=499, seed=5)
    assert single.t_stats[0] > 0
    assert single.stepm_p[0] == pytest.approx(single.spa_p_upper)


def test_results_are_reproducible_and_inputs_checked() -> None:
    data = noise_family(200, 4, seed=7)
    first, second = family_tests(data, n_boot=99, seed=1), family_tests(data, n_boot=99, seed=1)
    assert first.spa_p == second.spa_p
    np.testing.assert_array_equal(first.stepm_p, second.stepm_p)
    bad = data.copy()
    bad[0, 0] = np.nan
    with pytest.raises(ValueError, match="missing"):
        family_tests(bad, n_boot=99, seed=1)
    with pytest.raises(ValueError, match="two periods"):
        family_tests(data[:1], n_boot=99, seed=1)
