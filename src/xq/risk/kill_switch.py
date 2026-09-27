"""Kill switch and data-health breakers (RISK-006).

**Kill switch** (`KillSwitch`): a manual flag that stops new exposure. It is on while the
configured file exists, while the configured environment variable is set to a true value (``1``,
``true``, ``yes``, ``on``), or after `KillSwitch.engage` (the hook a later database flag or
operator command uses) until `KillSwitch.release`. With the profile's ``flatten`` policy, open
positions are closed too: the event engine sends a ``flat`` intent through the risk engine at the
first signal bar after the switch comes on. Reading the flag is I/O, so the engine reads it and
passes its reason to the risk engine in the market state; `RiskEngine.evaluate` stays pure.
Backtests run without a kill switch unless one is given: a flag on the machine running a
historical simulation says nothing about the past.

**Breakers** (`breaker_reasons`) stop new exposure while the data look unhealthy at a decision:

- **stale quote** — the latest quote is older than ``stale_quote_s`` seconds;
- **abnormal spread** — the latest spread is more than ``spread_multiple`` times the reference
  spread, the median spread of the last ``spread_window`` quotes (none until ten are known).

Neither blocks an exit: closing a position is always allowed (RISK-005).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from xq.core.config import BreakersConfig, KillSwitchConfig
from xq.risk.state import MarketState

_TRUE = {"1", "true", "yes", "on"}
#: Quotes needed before the reference spread is known.
MIN_SPREAD_QUOTES = 10


class KillSwitch:
    """The manual kill switch (module docstring)."""

    def __init__(
        self, config: KillSwitchConfig, *, environ: Mapping[str, str] | None = None
    ) -> None:
        self.config = config
        self.environ: Mapping[str, str] = os.environ if environ is None else environ
        self._manual: str | None = None

    @property
    def flatten(self) -> bool:
        """Whether open positions are closed while the switch is on."""
        return self.config.flatten

    def engage(self, reason: str) -> None:
        """Turn the switch on by hand (until `release`)."""
        self._manual = reason

    def release(self) -> None:
        """Turn off the manual flag (the file and the variable are the operator's)."""
        self._manual = None

    def reason(self) -> str | None:
        """Why the switch is on, or None."""
        if self._manual is not None:
            return f"kill switch: {self._manual}"
        path = self.config.file
        if path is not None and Path(path).exists():
            return f"kill switch: {path} exists"
        name = self.config.env_var
        if name is not None and self.environ.get(name, "").strip().lower() in _TRUE:
            return f"kill switch: {name} is set"
        return None


def breaker_reasons(market: MarketState, breakers: BreakersConfig) -> list[str]:
    """The data-health breakers in force at the decision (module docstring)."""
    reasons: list[str] = []
    if market.quote_age_s > breakers.stale_quote_s:
        reasons.append(
            f"stale quote: the latest is {market.quote_age_s:g} s old "
            f"(limit {breakers.stale_quote_s:g} s)"
        )
    reference = market.spread_reference
    if reference is not None and market.spread > breakers.spread_multiple * reference:
        reasons.append(
            f"abnormal spread: {market.spread:g} is more than {breakers.spread_multiple:g} x "
            f"the median {reference:g}"
        )
    return reasons
