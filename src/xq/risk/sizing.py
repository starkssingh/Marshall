"""Position sizing (RISK-002). Models never size; this module does, from the risk profile.

For an entry at reference price P with a stop at distance D (price units), equity E, contract
size C (units per lot) and the profile's `SizingConfig`:

- **fixed fractional** — ``risk_per_trade x E / (D x C)`` lots: a stop fill loses
  ``risk_per_trade`` of equity (before costs);
- **volatility targeting** — the exposure ``vol_target_annual / (sigma_daily x sqrt(periods))``
  of equity, i.e. ``exposure x E / (P x C)`` lots;
- the result is **capped by the strategy's requested exposure** (``requested x E / (P x C)``
  lots): a strategy can ask for less, never for more;
- scaled by the **calibrated win probability** — 0 at or below ``probability_zero``, 1 at or
  above ``probability_full``, linear between (only for intents that carry one; an uncalibrated
  probability never reaches sizing, RISK-005);
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


@dataclass(frozen=True)
class SizingResult:
    """The size of an entry and how it was reached (unsigned lots)."""

    lots: float
    raw_lots: float
    method_lots: float
    requested_lots: float
    probability_scale: float
    throttle: float

    def as_snapshot(self) -> dict[str, float | str]:
        """The sizing steps for a decision's audit snapshot."""
        return {
            "sized_lots": self.lots,
            "raw_lots": self.raw_lots,
            "method_lots": self.method_lots,
            "requested_lots": self.requested_lots,
            "probability_scale": self.probability_scale,
            "drawdown_throttle": self.throttle,
        }


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


def probability_scale(p: float | None, zero: float, full: float) -> float:
    """0 at or below `zero`, 1 at or above `full`, linear between; 1 without a probability."""
    if p is None:
        return 1.0
    return min(1.0, max(0.0, (p - zero) / (full - zero)))


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
    p_win: float | None,
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
    scale = probability_scale(p_win, config.probability_zero, config.probability_full)
    throttle = drawdown_throttle(drawdown, config.throttle_start, config.throttle_end)
    raw = min(method, requested) * scale * throttle
    lots = float(instrument.round_lots(f"{raw:.12f}"))
    return SizingResult(lots, raw, method, requested, scale, throttle)
