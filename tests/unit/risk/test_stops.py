"""RISK-004: intents without a usable stop are refused; a stop inside the spread's noise is
widened outward to the tick; one beyond the sigma bound, or unbounded for want of a sigma-hat, is
refused; targets and time stops must be on the right side."""

from typing import Literal

import pytest

from helpers.event_backtest import CFG
from xq.risk.stops import StopCheck, check_stops

POLICY = CFG.risk_config().stops
NOW = 1_710_000_000_000_000_000
HOUR = 3_600_000_000_000


def check(
    direction: Literal["long", "short"] = "long",
    stop: float | None = 1990.0,
    **changes: object,
) -> StopCheck:
    args: dict[str, object] = {
        "target": None,
        "time_stop": None,
        "now": NOW,
        "reference": 2000.1 if direction == "long" else 1999.9,
        "spread": 0.2,
        "sigma_daily": 0.01,
        "policy": POLICY,
        "tick": 0.01,
    }
    args.update(changes)
    return check_stops(direction, stop=stop, **args)  # type: ignore[arg-type]


def test_the_policy_is_the_provisional_profile() -> None:
    assert (POLICY.required, POLICY.min_spread_multiple, POLICY.max_sigma_multiple) == (
        True,
        3.0,
        5.0,
    )


def test_an_entry_without_a_stop_is_refused() -> None:
    result = check(stop=None)
    assert not result.accepted
    assert result.reasons == ("an entry needs a stop (RISK-004)",)
    relaxed = POLICY.model_copy(update={"required": False})
    assert check(stop=None, policy=relaxed) == StopCheck(None, 0.0)


def test_a_stop_within_bounds_is_kept() -> None:
    result = check(stop=1990.0)
    assert result.accepted
    assert result.notes == ()
    assert (result.stop, result.distance) == (1990.0, pytest.approx(10.1))
    short = check("short", stop=2010.0)
    assert (short.stop, short.distance) == (2010.0, pytest.approx(10.1))


def test_a_stop_on_the_wrong_side_is_refused() -> None:
    assert not check(stop=2000.1).accepted  # at the ask a long's stop is on the wrong side
    assert not check(stop=2001.0).accepted
    assert not check("short", stop=1999.9).accepted
    assert "losing side" in check("short", stop=1995.0).reasons[0]


def test_a_stop_closer_than_three_spreads_is_widened_outward_to_the_tick() -> None:
    # 3 x 0.2 = 0.6 from the ask
    result = check(stop=1999.8)
    assert result.accepted
    assert result.stop == 1999.5
    assert result.distance == pytest.approx(0.6)
    assert result.notes[0].startswith("stop widened from 1999.8 to 1999.5")
    assert check("short", stop=2000.0).stop == 2000.5
    # 3 x 0.235 = 0.705: 1999.395 is rounded away from the entry, to 1999.39 (never 1999.40)
    assert check(stop=2000.0, spread=0.235).stop == 1999.39
    assert check("short", stop=2000.0, spread=0.235).stop == 2000.61
    # no spread at all still leaves a tick between the entry and its stop
    assert check(stop=2000.09, spread=0.0).stop == 2000.09


def test_the_sigma_bound_is_inclusive_and_needs_a_sigma_hat() -> None:
    # 5 daily sigmas of 1 % at 2000 = 100
    assert check(stop=1900.0, reference=2000.0).accepted
    refused = check(stop=1899.99, reference=2000.0)
    assert not refused.accepted
    assert "exceeds 5 daily sigmas" in refused.reasons[0]
    assert check("short", stop=2100.0, reference=2000.0).accepted
    assert not check("short", stop=2100.01, reference=2000.0).accepted
    (reason,) = check(sigma_daily=None).reasons
    assert reason.startswith("no sigma-hat")
    assert not check(sigma_daily=0.0).accepted


def test_a_stop_widened_beyond_the_sigma_bound_is_refused() -> None:
    # sigma-hat so low that three spreads are more than five sigmas
    result = check(stop=1999.9, sigma_daily=0.00005)
    assert not result.accepted
    assert result.notes
    assert "exceeds" in result.reasons[0]


def test_targets_and_time_stops_must_be_on_the_right_side() -> None:
    assert check(target=2010.0).accepted
    assert not check(target=2000.1).accepted
    assert not check("short", stop=2010.0, target=2000.0).accepted
    assert check(time_stop=NOW + HOUR).accepted
    (reason,) = check(time_stop=NOW).reasons
    assert "time stop" in reason
