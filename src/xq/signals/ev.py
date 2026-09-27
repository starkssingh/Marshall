"""Expected value of a barrier trade in sigma-hat units (SIGNAL-002).

A candidate trade has a take-profit ``tp`` and a stop-loss ``sl`` at distances measured in units
of sigma-hat over the trade's horizon, and a calibrated probability ``p`` that the take-profit is
reached first. Its expected value per unit of sigma-hat is

    EV_gross = p x tp - (1 - p) x sl
    EV_net   = EV_gross - cost

where ``cost`` is the round-trip cost (spread, slippage, commissions) in the same sigma units
(`cost_in_sigmas`). The trade qualifies only if **EV_net > theta** and **p > p_min**, both strict;
theta and p_min are strategy parameters chosen on validation folds only (plan Phase 15).

The **conservative variant** replaces p by the lower confidence bound ``max(0, p - z x p_se)`` of
a forecast with standard error ``p_se`` in both EV and the p_min test, so an uncertain
probability needs a larger edge. Timing the barrier (which comes first when both could) is the
target's business (TGT-003); here p is taken as given, and only a **calibrated** p may be used —
the signal engine refuses uncalibrated forecasts before this point (SIGNAL-004).
"""

from __future__ import annotations

from dataclasses import dataclass

_BPS = 1e-4


@dataclass(frozen=True)
class ExpectedValue:
    """One candidate's expected value and whether it qualifies (module docstring)."""

    p: float
    p_used: float
    tp: float
    sl: float
    cost: float
    gross: float
    net: float
    theta: float
    p_min: float
    conservative: bool
    reasons: tuple[str, ...]

    @property
    def qualifies(self) -> bool:
        """EV_net > theta and p > p_min (with p the conservative bound when used)."""
        return not self.reasons

    @property
    def payoff_ratio(self) -> float:
        """Take-profit over stop-loss distance."""
        return self.tp / self.sl


def ev_gross(p: float, tp: float, sl: float) -> float:
    """``p x tp - (1 - p) x sl``."""
    return p * tp - (1 - p) * sl


def lower_bound(p: float, p_se: float, z: float) -> float:
    """The conservative probability ``max(0, p - z x p_se)``."""
    if p_se < 0 or z < 0:
        raise ValueError("p_se and z must not be negative")
    return max(0.0, p - z * p_se)


def cost_in_sigmas(cost_bps: float, sigma: float) -> float:
    """A round-trip cost in basis points of price as a multiple of sigma-hat (a price fraction)."""
    if sigma <= 0:
        raise ValueError("sigma-hat must be positive")
    return cost_bps * _BPS / sigma


def expected_value(
    p: float,
    *,
    tp: float,
    sl: float,
    cost: float,
    theta: float,
    p_min: float,
    p_se: float | None = None,
    z: float | None = None,
) -> ExpectedValue:
    """The expected value of a candidate and whether it qualifies (module docstring).

    Args:
        p: Calibrated probability that the take-profit comes first.
        tp: Take-profit distance in sigma-hat units (> 0).
        sl: Stop-loss distance in sigma-hat units (> 0).
        cost: Round-trip cost in sigma-hat units (>= 0).
        theta: Minimum net EV in sigma-hat units (strict).
        p_min: Minimum probability (strict).
        p_se: Standard error of `p`; with `z`, selects the conservative variant.
        z: Normal quantile of the lower confidence bound (e.g. 1.645 for 95 % one-sided).

    Raises:
        ValueError: for a probability outside [0, 1], non-positive distances, a negative cost,
            or only one of `p_se` and `z`.
    """
    if not 0 <= p <= 1:
        raise ValueError(f"p must be a probability, got {p}")
    if tp <= 0 or sl <= 0:
        raise ValueError("take-profit and stop-loss distances must be positive")
    if cost < 0:
        raise ValueError("the cost must not be negative")
    if (p_se is None) != (z is None):
        raise ValueError("the conservative variant needs both p_se and z")
    conservative = p_se is not None and z is not None
    p_used = lower_bound(p, p_se, z) if p_se is not None and z is not None else p
    gross = ev_gross(p_used, tp, sl)
    net = gross - cost
    reasons: list[str] = []
    if not net > theta:
        reasons.append(f"EV_net {net:.4f} is not above theta {theta:g} (sigma units)")
    if not p_used > p_min:
        which = "the lower bound of p" if conservative else "p"
        reasons.append(f"{which} {p_used:.4f} is not above p_min {p_min:g}")
    return ExpectedValue(
        p=p,
        p_used=p_used,
        tp=tp,
        sl=sl,
        cost=cost,
        gross=gross,
        net=net,
        theta=theta,
        p_min=p_min,
        conservative=conservative,
        reasons=tuple(reasons),
    )
