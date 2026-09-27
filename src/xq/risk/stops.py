"""Stop policy (RISK-004): every long or short intent carries a stop the risk engine can bound.

For an intent on side ``long`` or ``short`` with entry reference price R — the side's quote for a
market entry (the ask for a long, the bid for a short), the order's price for a limit or stop
entry — and the profile's `StopPolicyConfig`:

- a **stop is required** (``required``); without one the intent is refused;
- the stop must be on the **losing side** of R (below it for a long, above it for a short);
- a stop **closer** than ``min_spread_multiple`` x the current spread (and at least one tick) is
  **widened** to that distance, rounded outward to the tick — the decision's ``adjusted_stop`` —
  so a stop is never inside the noise of the spread; sizing then uses the widened distance, so the
  risk to the stop stays within budget;
- a stop **farther** than ``max_sigma_multiple`` x daily sigma-hat x R is **refused**, and so is
  any stop when no sigma-hat is known at the decision: its distance could not be bounded;
- a **target**, if any, must be on the winning side of R, and a **time stop**, if any, after the
  decision time. Time stops are allowed in addition to the price stop, never instead of it: the
  size is set by the distance to the price stop (RISK-002).

Distances are in price units computed from volatility units and spreads at the decision, never
fixed dollar amounts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from xq.core.config import StopPolicyConfig

_EPS = 1e-9


@dataclass(frozen=True)
class StopCheck:
    """The stop policy's answer: the stop the decision may use, or why there is none."""

    stop: float | None
    distance: float
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        """True when nothing refuses the intent's stops."""
        return not self.reasons


def check_stops(
    direction: Literal["long", "short"],
    *,
    stop: float | None,
    target: float | None,
    time_stop: int | None,
    now: int,
    reference: float,
    spread: float,
    sigma_daily: float | None,
    policy: StopPolicyConfig,
    tick: float,
) -> StopCheck:
    """Apply the stop policy (module docstring) to one intent's stop, target and time stop.

    Args:
        direction: The intent's side.
        stop: The intent's stop price, if any.
        target: The intent's target price, if any.
        time_stop: The intent's time stop (UTC ns), if any.
        now: The decision time (UTC ns).
        reference: The entry reference price R.
        spread: The spread of the latest quote.
        sigma_daily: Daily sigma-hat as a fraction of price, if one is known at `now`.
        policy: The profile's stop policy.
        tick: The instrument's tick size.
    """
    long = direction == "long"
    reasons: list[str] = []
    if target is not None and (target <= reference if long else target >= reference):
        reasons.append(
            f"the target {target:g} is not on the winning side of the entry reference {reference:g}"
        )
    if time_stop is not None and time_stop <= now:
        reasons.append("the time stop is not after the decision time")
    if stop is None:
        if policy.required:
            reasons.append("an entry needs a stop (RISK-004)")
        return StopCheck(None, 0.0, tuple(reasons))
    if stop >= reference if long else stop <= reference:
        reasons.append(
            f"the stop {stop:g} is not on the losing side of the entry reference {reference:g}"
        )
        return StopCheck(None, 0.0, tuple(reasons))
    notes: list[str] = []
    distance = abs(reference - stop)
    minimum = max(policy.min_spread_multiple * spread, tick)
    if distance < minimum:
        widened = _outward(reference - minimum if long else reference + minimum, tick, long)
        notes.append(
            f"stop widened from {stop:g} to {widened:g}: closer than "
            f"{policy.min_spread_multiple:g} spreads ({minimum:g})"
        )
        stop, distance = widened, abs(reference - widened)
    if sigma_daily is None or not sigma_daily > 0:
        reasons.append("no sigma-hat at the decision: the stop distance cannot be bounded")
    else:
        maximum = policy.max_sigma_multiple * sigma_daily * reference
        if distance > maximum * (1 + _EPS):
            reasons.append(
                f"the stop distance {distance:g} exceeds {policy.max_sigma_multiple:g} daily "
                f"sigmas ({maximum:g})"
            )
    if reasons:
        return StopCheck(None, 0.0, tuple(reasons), tuple(notes))
    return StopCheck(stop, distance, (), tuple(notes))


def _outward(level: float, tick: float, long: bool) -> float:
    """`level` rounded to the tick away from the entry: down for a long's stop, up for a short's."""
    steps = level / tick
    rounded = math.floor(steps + _EPS) if long else math.ceil(steps - _EPS)
    decimals = max(0, -math.floor(math.log10(tick))) + 2
    return round(rounded * tick, decimals)
