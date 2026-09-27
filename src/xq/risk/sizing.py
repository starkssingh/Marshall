"""Position sizing (RISK-002). Models never size; this module does, from the risk profile.

For an entry at reference price P with a stop at distance D (price units), equity E, contract
size C (units per lot) and the profile's `SizingConfig`:

- **fixed fractional** — ``risk_per_trade x E / (D x C)`` lots: a stop fill loses
  ``risk_per_trade`` of equity (before costs);
- **volatility targeting** — the exposure ``vol_target_annual / (sigma_daily x sqrt(periods))``
  of equity, i.e. ``exposure x E / (P x C)`` lots;
- the result is **capped by the strategy's requested exposure** (``requested x E / (P x C)``
  lots): a strategy can ask for less, never for more;
- scaled, for an intent with a calibrated win probability, by its **edge per unit of risk**
  (ADR 0053): with ``p_lcb = max(0, p - lcb_z x p_se)`` the probability's lower confidence bound
  (as in SIGNAL-002), TP and SL the distances from the entry to the target and to the stop, and
  ``cost`` the round-trip cost in price,

      ev_r = p_lcb x TP/SL - (1 - p_lcb) - cost/SL

  the expected profit in units of the amount at risk, and the multiplier is
  ``clip(ev_r / ev_r_full, 0, 1)``: zero at or below break-even whatever the payoff ratio, full
  once the edge reaches ``ev_r_full`` (an uncalibrated probability never reaches sizing,
  RISK-005; an intent without one is not scaled);
- scaled by the **drawdown throttle** — 1 up to ``throttle_start``, falling linearly to 0 at
  ``throttle_end``;
- **rounded down** to the lot step, capped at the instrument's maximum lot, and zero below its
  minimum lot (`InstrumentSpec.round_lots`).

Every factor can only shrink the size. The limits of RISK-003 are applied afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from xq.core.config import InstrumentSpec, SizingConfig
from xq.signals.ev import lower_bound


@dataclass(frozen=True)
class Edge:
    """What the edge of an entry is priced from (module docstring); distances in price units."""

    p: float
    p_se: float
    target_distance: float
    cost: float

    def __post_init__(self) -> None:
        if not 0 <= self.p <= 1:
            raise ValueError(f"p must be a probability, got {self.p}")
        if self.p_se < 0 or self.target_distance <= 0 or self.cost < 0:
            raise ValueError("p_se and cost must not be negative; the target distance positive")


@dataclass(frozen=True)
class SizingResult:
    """The size of an entry and how it was reached (unsigned lots)."""

    lots: float
    raw_lots: float
    method_lots: float
    requested_lots: float
    edge_scale: float
    throttle: float
    p_lcb: float | None = None
    ev_r: float | None = None

    def as_snapshot(self) -> dict[str, float | str]:
        """The sizing steps for a decision's audit snapshot."""
        snapshot: dict[str, float | str] = {
            "sized_lots": self.lots,
            "raw_lots": self.raw_lots,
            "method_lots": self.method_lots,
            "requested_lots": self.requested_lots,
            "edge_scale": self.edge_scale,
            "drawdown_throttle": self.throttle,
        }
        if self.p_lcb is not None and self.ev_r is not None:
            snapshot.update(p_lcb=self.p_lcb, ev_r=self.ev_r)
        return snapshot


def fixed_fractional_lots(
    equity: float, risk_fraction: float, stop_distance: float, contract_size: float
) -> float:
    """Lots whose loss at the stop is `risk_fraction` of `equity`."""
    if stop_distance <= 0:
        raise ValueError("the stop distance must be positive")
    return max(0.0, risk_fraction * equity / (stop_distance * contract_size))


def vol_target_lots(
    equity: float,
    annual_target: float,
    sigma_daily: float,
    periods_per_year: int,
    price: float,
    contract_size: float,
) -> float:
    """Lots whose annualized volatility is `annual_target` of `equity`."""
    if sigma_daily <= 0:
        raise ValueError("sigma-hat must be positive")
    exposure = annual_target / (sigma_daily * math.sqrt(periods_per_year))
    return max(0.0, exposure * equity / (price * contract_size))


def edge_per_risk(p_lcb: float, payoff_ratio: float, cost_r: float) -> float:
    """``p_lcb x TP/SL - (1 - p_lcb) - cost/SL``: the expected profit per unit of risk."""
    return p_lcb * payoff_ratio - (1 - p_lcb) - cost_r


def edge_scale(ev_r: float, ev_r_full: float) -> float:
    """``clip(ev_r / ev_r_full, 0, 1)``."""
    if ev_r_full <= 0:
        raise ValueError("ev_r_full must be positive")
    return min(1.0, max(0.0, ev_r / ev_r_full))


def drawdown_throttle(drawdown: float, start: float, end: float) -> float:
    """1 up to `start`, 0 from `end` on, linear between."""
    if drawdown <= start:
        return 1.0
    if drawdown >= end:
        return 0.0
    return (end - drawdown) / (end - start)


def size_entry(
    config: SizingConfig,
    instrument: InstrumentSpec,
    *,
    equity: float,
    price: float,
    stop_distance: float,
    sigma_daily: float | None,
    periods_per_year: int,
    requested_exposure: float,
    edge: Edge | None,
    drawdown: float,
) -> SizingResult:
    """The unsigned size of an entry (module docstring).

    Raises:
        ValueError: for volatility targeting without a positive sigma-hat, or a non-positive
            stop distance for fixed-fractional sizing.
    """
    contract = float(instrument.contract_size)
    if equity <= 0:
        method = 0.0
    elif config.method == "fixed_fractional":
        method = fixed_fractional_lots(equity, config.risk_per_trade, stop_distance, contract)
    else:
        if sigma_daily is None:
            raise ValueError("volatility targeting needs a sigma-hat")
        method = vol_target_lots(
            equity, config.vol_target_annual, sigma_daily, periods_per_year, price, contract
        )
    requested = max(0.0, requested_exposure * equity / (price * contract))
    scale, p_lcb, ev_r = 1.0, None, None
    if edge is not None:
        if stop_distance <= 0:
            raise ValueError(
                "the edge is priced per unit of risk: the stop distance must be positive"
            )
        p_lcb = lower_bound(edge.p, edge.p_se, config.lcb_z)
        ev_r = edge_per_risk(p_lcb, edge.target_distance / stop_distance, edge.cost / stop_distance)
        scale = edge_scale(ev_r, config.ev_r_full)
    throttle = drawdown_throttle(drawdown, config.throttle_start, config.throttle_end)
    raw = min(method, requested) * scale * throttle
    lots = float(instrument.round_lots(f"{raw:.12f}"))
    return SizingResult(lots, raw, method, requested, scale, throttle, p_lcb, ev_r)
