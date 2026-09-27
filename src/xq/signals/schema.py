"""Decision-chain schemas: trade intent, risk decision and order intent (plan Phase 15).

A strategy never sizes a position and never places an order. It emits a `TradeIntent` — the
position it wants (direction and requested exposure), how to enter and where its stop, target
and time stop are. The risk layer turns an intent into a `RiskDecision` (approved or rejected,
with reasons, the size in lots and the stop it accepts). Only an **approved** decision can become
an `OrderIntent`: the order type refuses to be built without one, and its side, size, stop and
target must equal the decision's.

Sprint 11 builds the three schemas the event backtester needs, in the plan's shape (section 6,
Phase 15) plus the fields the engine uses: the engine stamps ``intent_id`` and ``created_at`` (the
decision time) on every intent, so a strategy cannot choose its own decision time; the requested
``exposure`` (a fraction of capital, the screener's convention) is what the Sprint 11 placeholder
approver sizes from. SIGNAL-001 (Sprint 12) completes the schema set with forecasts, candidates
and audit records and exports JSON Schemas; RISK-005 replaces the placeholder approver
(ADR 0048, ADR 0049).

Prices (entry, stop, target) are absolute price levels computed by the strategy at the decision
time from volatility units or basis points: parameters are never fixed dollar distances.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from xq.core.types import Side

Direction = Literal["long", "short", "flat"]
EntryType = Literal["market", "limit", "stop"]


class TradeIntent(BaseModel):
    """The position a strategy wants, before any risk decision (module docstring).

    ``flat`` closes the position (market order, no exposure, no bracket). ``long`` and ``short``
    ask for ``exposure`` (> 0, a fraction of capital) on that side, entered by ``entry_type``;
    ``limit``/``stop`` entries need ``limit_price``. ``stop`` and ``target`` form an OCO bracket
    on the whole resulting position; ``time_stop`` closes it at that instant (or at the next
    open).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: Direction
    exposure: float = Field(default=0.0, ge=0)
    entry_type: EntryType = "market"
    limit_price: float | None = Field(default=None, gt=0)
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    time_stop: AwareDatetime | None = None
    strategy_id: str = Field(default="strategy", min_length=1)
    signal_id: str | None = None
    reason: str = ""
    intent_id: str | None = None
    created_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TradeIntent:
        if self.direction == "flat":
            extras = [
                name
                for name in ("limit_price", "stop", "target", "time_stop")
                if getattr(self, name) is not None
            ]
            if self.exposure != 0 or self.entry_type != "market" or extras:
                raise ValueError("a flat intent is a market exit without exposure or bracket")
            return self
        if self.exposure <= 0:
            raise ValueError(f"a {self.direction} intent needs a positive exposure")
        if (self.entry_type == "market") != (self.limit_price is None):
            raise ValueError("limit and stop entries need limit_price; market entries take none")
        below, above = (self.stop, self.target)
        if self.direction == "short":
            below, above = above, below
        if below is not None and above is not None and below >= above:
            raise ValueError(f"a {self.direction} intent's stop and target are on the wrong sides")
        return self

    @property
    def sign(self) -> int:
        """+1 long, -1 short, 0 flat."""
        return {"long": 1, "short": -1, "flat": 0}[self.direction]


class RiskDecision(BaseModel):
    """The risk layer's answer to one intent (plan RISK-005 fields plus the approved position).

    ``target_lots`` is the signed position the decision approves after the order;
    ``size_lots`` and ``side`` are the order that gets there from the position the decision saw.
    An approved decision with ``size_lots == 0`` needs no order. A rejected one has no size.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    decided_at: AwareDatetime
    approved: bool
    side: Side | None = None
    size_lots: float = Field(default=0.0, ge=0)
    target_lots: float = 0.0
    adjusted_stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    reasons: tuple[str, ...] = ()
    limits_snapshot: dict[str, float | str] = {}
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> RiskDecision:
        if not self.approved and (self.size_lots != 0 or self.side is not None):
            raise ValueError("a rejected decision carries no order size or side")
        if (self.size_lots > 0) != (self.side is not None):
            raise ValueError("an order size needs a side and a side needs a size")
        if not self.approved and not self.reasons:
            raise ValueError("a rejected decision must give its reasons")
        return self


class OrderIntent(BaseModel):
    """An order to the broker; it can only be built from an approved `RiskDecision`.

    ``expected_position_lots`` is the position the decision saw: the broker refuses the order if
    the position has changed by the time it arrives, so an order never trades from a position
    its decision did not size for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: RiskDecision
    order_id: str = Field(min_length=1)
    side: Side
    size_lots: float = Field(gt=0)
    order_type: EntryType
    price: float | None = Field(default=None, gt=0)
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    time_stop: AwareDatetime | None = None
    expected_position_lots: float
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def _from_an_approved_decision(self) -> OrderIntent:
        decision = self.decision
        if not decision.approved:
            raise ValueError("an order can only be built from an approved risk decision")
        if decision.side is not self.side or decision.size_lots != self.size_lots:
            raise ValueError("the order's side and size must be the risk decision's")
        if self.stop != decision.adjusted_stop or self.target != decision.target:
            raise ValueError("the order's stop and target must be the risk decision's")
        if (self.order_type == "market") != (self.price is None):
            raise ValueError("limit and stop orders need a price; market orders take none")
        return self

    @classmethod
    def from_decision(
        cls, decision: RiskDecision, intent: TradeIntent, *, expected_position_lots: float
    ) -> OrderIntent:
        """The order an approved decision allows for `intent`.

        Raises:
            ValueError: if the decision is rejected, needs no order, or is for another intent.
        """
        if decision.intent_id != intent.intent_id:
            raise ValueError("the decision was taken for another intent")
        if decision.side is None:
            raise ValueError("the decision needs no order")
        order_id = f"O-{decision.intent_id}"
        return cls(
            decision=decision,
            order_id=order_id,
            side=decision.side,
            size_lots=decision.size_lots,
            order_type=intent.entry_type,
            price=intent.limit_price,
            stop=decision.adjusted_stop,
            target=decision.target,
            time_stop=intent.time_stop,
            expected_position_lots=expected_position_lots,
            idempotency_key=order_id,
        )

    @property
    def decision_id(self) -> str:
        """The id of the risk decision that approved this order."""
        return self.decision.decision_id

    @property
    def intent_id(self) -> str:
        """The id of the intent the order serves."""
        return self.decision.intent_id

    @property
    def signed_lots(self) -> float:
        """The order size with its sign (positive buys)."""
        return self.side.sign * self.size_lots
