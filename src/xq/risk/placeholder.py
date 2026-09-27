"""PLACEHOLDER pass-through risk approver — **not a risk engine** (Sprint 11, ADR 0048, ADR 0049).

The event backtester routes every intent through a risk approver, and the ledger links every
order to the decision that approved it. Until `RiskEngine.evaluate` exists (RISK-005, Sprint 12),
this placeholder stands in for it. It performs **no risk checks**: no limits, no halts, no stop
policy, no drawdown throttle. It approves every intent and sizes it mechanically:

- target position = sign * requested exposure * capital / (mid * contract size), with the mid of
  the latest quote known at the decision time and the backtest's initial capital (the screener's
  convention, BT-002), rounded **down** to the lot step within the instrument's minimum and
  maximum lot (`InstrumentSpec.round_lots`);
- the order is the difference between that target and the position the decision sees;
- the intent's stop and target are passed through unchanged.

Every decision it writes says so: its ``config_version`` is `PLACEHOLDER_CONFIG_VERSION` and its
first reason `PLACEHOLDER_REASON`, and every event-backtest result and report carries
`PLACEHOLDER_LABEL`. Results produced with it are engineering tests of the execution simulator,
not evidence about any strategy.
"""

from __future__ import annotations

from xq.backtest.events import AccountState, Quote, clean_lots
from xq.core.config import InstrumentSpec
from xq.core.types import Side
from xq.signals.schema import RiskDecision, TradeIntent

PLACEHOLDER_CONFIG_VERSION = "placeholder-pass-through-1"
PLACEHOLDER_REASON = (
    "PLACEHOLDER pass-through approver: no risk checks (RiskEngine is RISK-005, Sprint 12)"
)
PLACEHOLDER_LABEL = (
    "PLACEHOLDER pass-through risk approver - no risk checks until RISK-005 (Sprint 12)"
)


class PassThroughRiskApprover:
    """Approves every intent with mechanical sizing and no risk checks (module docstring)."""

    label = PLACEHOLDER_LABEL
    config_version = PLACEHOLDER_CONFIG_VERSION

    def __init__(self, instrument: InstrumentSpec, capital: float) -> None:
        if capital <= 0:
            raise ValueError("capital must be positive")
        self.instrument = instrument
        self.capital = float(capital)

    def evaluate(
        self, intent: TradeIntent, account: AccountState, quote: Quote | None
    ) -> RiskDecision:
        """The (always approving, unless no quote is known) placeholder decision for `intent`.

        Raises:
            ValueError: if the engine has not stamped the intent's id and decision time.
        """
        if intent.intent_id is None or intent.created_at is None:
            raise ValueError("the engine stamps intent_id and created_at before the risk decision")
        decision_id = f"D-{intent.intent_id}"
        if quote is None:
            return RiskDecision(
                decision_id=decision_id,
                intent_id=intent.intent_id,
                decided_at=intent.created_at,
                approved=False,
                reasons=(PLACEHOLDER_REASON, "no quote known at the decision time"),
                config_version=self.config_version,
            )
        mid = quote.mid
        contract = float(self.instrument.contract_size)
        requested = intent.exposure * self.capital / (mid * contract)
        target = intent.sign * float(self.instrument.round_lots(requested))
        size = clean_lots(target - account.position_lots)
        reasons = [PLACEHOLDER_REASON]
        if size == 0:
            reasons.append("target position unchanged: no order")
        return RiskDecision(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            decided_at=intent.created_at,
            approved=True,
            side=None if size == 0 else (Side.BUY if size > 0 else Side.SELL),
            size_lots=abs(size),
            target_lots=clean_lots(target),
            adjusted_stop=intent.stop,
            target=intent.target,
            reasons=tuple(reasons),
            limits_snapshot={
                "capital": self.capital,
                "reference_mid": mid,
                "requested_exposure": intent.exposure,
                "requested_lots": requested,
            },
            config_version=self.config_version,
        )
