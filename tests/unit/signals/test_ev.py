"""SIGNAL-002: golden expected-value cases — gross and net EV in sigma units, the conservative
lower bound of p, strict thresholds, and costs converted to sigma units."""

import pytest

from xq.signals.ev import cost_in_sigmas, ev_gross, expected_value, lower_bound


def test_golden_ev_with_a_calibrated_probability() -> None:
    # 0.6 x 2 - 0.4 x 1 = 0.8 gross; less 0.2 of costs = 0.6 net
    ev = expected_value(0.6, tp=2.0, sl=1.0, cost=0.2, theta=0.1, p_min=0.55)
    assert ev.gross == pytest.approx(0.8)
    assert ev.net == pytest.approx(0.6)
    assert ev.p_used == 0.6
    assert ev.payoff_ratio == 2.0
    assert ev.qualifies
    assert not ev.conservative


def test_golden_ev_with_the_conservative_lower_bound() -> None:
    # p_low = 0.6 - 1.645 x 0.05 = 0.51775; 0.51775 x 2 - 0.48225 x 1 = 0.55325; net 0.35325
    ev = expected_value(0.6, tp=2.0, sl=1.0, cost=0.2, theta=0.1, p_min=0.5, p_se=0.05, z=1.645)
    assert ev.p_used == pytest.approx(0.51775)
    assert ev.gross == pytest.approx(0.55325)
    assert ev.net == pytest.approx(0.35325)
    assert ev.qualifies
    assert ev.conservative
    # the bound, not p, faces p_min
    stricter = expected_value(
        0.6, tp=2.0, sl=1.0, cost=0.2, theta=0.1, p_min=0.55, p_se=0.05, z=1.645
    )
    assert not stricter.qualifies
    assert stricter.reasons == ("the lower bound of p 0.5177 is not above p_min 0.55",)


def test_symmetric_barriers_without_edge_lose_the_costs() -> None:
    ev = expected_value(0.5, tp=1.0, sl=1.0, cost=0.1, theta=0.0, p_min=0.5)
    assert ev.gross == 0.0
    assert ev.net == pytest.approx(-0.1)
    assert len(ev.reasons) == 2  # neither EV nor p clears its threshold


def test_thresholds_are_strict() -> None:
    # EV_net exactly theta does not qualify; p exactly p_min does not either
    at_theta = expected_value(0.6, tp=2.0, sl=1.0, cost=0.2, theta=0.6, p_min=0.5)
    assert at_theta.net == pytest.approx(0.6)
    assert not at_theta.qualifies
    assert at_theta.reasons[0].startswith("EV_net 0.6000 is not above theta 0.6")
    at_p_min = expected_value(0.6, tp=2.0, sl=1.0, cost=0.0, theta=0.0, p_min=0.6)
    assert at_p_min.reasons == ("p 0.6000 is not above p_min 0.6",)


def test_helpers_and_input_checks() -> None:
    assert ev_gross(0.25, 3.0, 1.0) == 0.0  # a fair 3:1 bet
    assert lower_bound(0.05, 0.1, 1.645) == 0.0  # never below zero
    # a 2 bp round trip against a 0.5 % sigma-hat is 0.04 sigmas
    assert cost_in_sigmas(2.0, 0.005) == pytest.approx(0.04)
    with pytest.raises(ValueError, match="probability"):
        expected_value(1.2, tp=1.0, sl=1.0, cost=0.0, theta=0.0, p_min=0.5)
    with pytest.raises(ValueError, match="both p_se and z"):
        expected_value(0.6, tp=1.0, sl=1.0, cost=0.0, theta=0.0, p_min=0.5, p_se=0.1)
    with pytest.raises(ValueError, match="positive"):
        expected_value(0.6, tp=0.0, sl=1.0, cost=0.0, theta=0.0, p_min=0.5)
    with pytest.raises(ValueError, match="sigma-hat"):
        cost_in_sigmas(2.0, 0.0)
