"""Decision-chain schemas: trade intent, risk decision and order intent (plan Phase 15).

A strategy never sizes a position and never places an order. It emits a `TradeIntent` — the
position it wants (direction and requested exposure), how to enter, where its stop, target and
time stop are and, when a model stands behind it, the calibrated probability of a win. The risk
engine (`xq.risk.engine.RiskEngine`, RISK-005) turns an intent into a `RiskDecision` (approved or
rejected, with reasons, the size in lots and the stop it accepts). Only an **approved decision
issued by the risk engine** can become an `OrderIntent` (the plan's construction rule):

- a decision carries a private *issued* mark that only `xq.risk.engine.issue_decision` sets; a
  decision built any other way — by hand, validated from a dict, or copied with `model_copy` —
  is not issued;
- the order type refuses a decision that is not issued or not approved, and its side, size,
  order type, price, stop and target must equal the decision's; `OrderIntent.model_construct`
  (which skips validation) and `OrderIntent.model_copy(update=...)` are disabled;
- an architectural test (``tests/unit/risk/test_architecture.py``) fails if code under ``src/``
  builds an `OrderIntent` other than through `OrderIntent.from_decision`, or issues a decision
  outside the risk engine.

The engine stamps ``intent_id`` and ``created_at`` (the decision time) on every intent, so a
strategy cannot choose its own decision time. ``exposure`` is the requested exposure, a fraction
of equity: a cap on the size, never the size (RISK-002).

Upstream of the intent (SIGNAL-001, plan Phase 15): a model's `Forecast`, the filtered
`RegimeState`, the `SignalCandidate` the signal engine builds from them and the `SignalRecord`
that audits every candidate, whatever became of it. All schemas are frozen and exported as JSON
Schemas to ``docs/specs/interfaces/`` (`write_json_schemas`, or ``uv run python -m
xq.signals.schema docs/specs/interfaces``); a test fails when the committed files are stale.

Prices (entry, stop, target) are absolute price levels computed by the strategy at the decision
time from volatility units or basis points: parameters are never fixed dollar distances.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from xq.core.types import Side

Direction = Literal["long", "short", "flat"]
EntryType = Literal["market", "limit", "stop"]


class TradeIntent(BaseModel):
    """The position a strategy wants, before any risk decision (module docstring).

    ``flat`` closes the position (market order, no exposure, no bracket). ``long`` and ``short``
    ask for ``exposure`` (> 0, a fraction of equity) on that side, entered by ``entry_type``;
    ``limit``/``stop`` entries need ``limit_price``. ``stop`` and ``target`` form an OCO bracket
    on the whole resulting position; ``time_stop`` closes it at that instant (or at the next
    open). ``p_win`` is the probability that the trade reaches its target first and ``p_se`` its
    standard error; the risk engine sizes on the edge they imply (its lower confidence bound
    against the target, the stop and the costs, ADR 0053) only when ``calibrated`` is true, and
    refuses an uncalibrated probability, or one without a standard error or a target.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: Direction
    exposure: float = Field(default=0.0, ge=0)
    entry_type: EntryType = "market"
    limit_price: float | None = Field(default=None, gt=0)
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    time_stop: AwareDatetime | None = None
    p_win: float | None = Field(default=None, ge=0, le=1)
    p_se: float | None = Field(default=None, ge=0)
    calibrated: bool = False
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
                for name in ("limit_price", "stop", "target", "time_stop", "p_win", "p_se")
                if getattr(self, name) is not None
            ]
            if self.exposure != 0 or self.entry_type != "market" or extras:
                raise ValueError("a flat intent is a market exit without exposure or bracket")
            return self
        if self.exposure <= 0:
            raise ValueError(f"a {self.direction} intent needs a positive exposure")
        if self.p_se is not None and self.p_win is None:
            raise ValueError("p_se is the standard error of p_win: it needs p_win")
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
    """The risk engine's answer to one intent (plan RISK-005 fields plus the approved order).

    ``target_lots`` is the signed position the decision approves after the order;
    ``size_lots``, ``side``, ``order_type`` and ``price`` are the order that gets there from the
    position the decision saw. An approved decision with ``size_lots == 0`` needs no order. A
    rejected one has no size. Only decisions issued by the risk engine can back an order (module
    docstring): `issued` says whether this one was.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    _issued: bool = PrivateAttr(default=False)

    decision_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    decided_at: AwareDatetime
    approved: bool
    side: Side | None = None
    size_lots: float = Field(default=0.0, ge=0)
    target_lots: float = 0.0
    order_type: EntryType = "market"
    price: float | None = Field(default=None, gt=0)
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
        if (self.order_type == "market") != (self.price is None):
            raise ValueError("limit and stop orders need a price; market orders take none")
        return self

    @property
    def issued(self) -> bool:
        """True only for a decision issued by the risk engine (not a copy or a hand-built one)."""
        return self._issued

    def model_copy(self, *, update: Any = None, deep: bool = False) -> Self:
        """A copy, which is never issued: only the risk engine issues decisions."""
        copy = super().model_copy(update=update, deep=deep)
        copy._issued = False
        return copy


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
        if not decision.issued:
            raise ValueError("an order can only be built from a decision the risk engine issued")
        if decision.side is not self.side or decision.size_lots != self.size_lots:
            raise ValueError("the order's side and size must be the risk decision's")
        if self.order_type != decision.order_type or self.price != decision.price:
            raise ValueError("the order's type and price must be the risk decision's")
        if self.stop != decision.adjusted_stop or self.target != decision.target:
            raise ValueError("the order's stop and target must be the risk decision's")
        return self

    @classmethod
    def model_construct(  # type: ignore[override]  # the pydantic plugin types it per field
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        """Disabled: an order is never built without validating its risk decision."""
        raise TypeError("OrderIntent.model_construct would skip the risk decision check")

    def model_copy(self, *, update: Any = None, deep: bool = False) -> Self:
        """A copy of the same order; changing any field is refused (build a new order instead)."""
        if update:
            raise TypeError("an OrderIntent cannot be changed after its risk decision")
        return super().model_copy(deep=deep)

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
            order_type=decision.order_type,
            price=decision.price,
            stop=decision.adjusted_stop,
            target=decision.target,
            time_stop=intent.time_stop if decision.target_lots != 0 else None,
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


# --- forecasts, regimes, candidates and audit records (SIGNAL-001) ------------------------------


class Forecast(BaseModel):
    """A model's forecast at decision time ``ts`` (plan Phase 15), the input of the signal engine.

    ``p_tp_first`` is the probability that a trade on ``side`` reaches its take-profit before its
    stop-loss within ``horizon`` (the barrier target the model was trained on, ``target_id``);
    ``p_up`` the probability of a positive return over the horizon; ``exp_return`` and
    ``quantiles`` (keys in (0, 1)) the expected return and its quantiles, as fractions of price.
    At least one must be given. ``sigma_hat`` is the model's sigma-hat over the horizon (a
    fraction of price). ``calibrated`` says whether the probabilities went through a calibration
    fitted on training or validation data only (``calibration_id``); ``p_se`` is the standard error
    of ``p_tp_first`` for the conservative EV variant. The signal engine refuses uncalibrated
    forecasts (SIGNAL-004).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    forecast_id: str = Field(min_length=1)
    ts: AwareDatetime
    instrument: str = Field(min_length=1)
    horizon: timedelta
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    feature_set_version: str = Field(min_length=1)
    target_id: str | None = None
    side: Literal["long", "short"] = "long"
    p_up: float | None = Field(default=None, ge=0, le=1)
    p_tp_first: float | None = Field(default=None, ge=0, le=1)
    p_se: float | None = Field(default=None, ge=0)
    exp_return: float | None = None
    quantiles: dict[float, float] | None = None
    sigma_hat: float = Field(gt=0)
    calibrated: bool
    calibration_id: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Forecast:
        if self.horizon <= timedelta(0):
            raise ValueError("a forecast horizon must be positive")
        outputs = (self.p_up, self.p_tp_first, self.exp_return, self.quantiles)
        if all(value is None for value in outputs):
            raise ValueError("a forecast needs p_up, p_tp_first, exp_return or quantiles")
        if self.quantiles is not None and not all(0 < q < 1 for q in self.quantiles):
            raise ValueError("quantile levels must lie in (0, 1)")
        if self.calibrated and self.calibration_id is None:
            raise ValueError("a calibrated forecast names its calibration (calibration_id)")
        return self


class RegimeState(BaseModel):
    """The filtered regime at ``ts`` (never a smoothed or Viterbi state; plan Phase 15).

    ``probs`` are the regime probabilities given data up to ``ts`` (summing to one), ``label``
    the regime they assign and ``age_bars`` how many bars that label has held.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: AwareDatetime
    model_version: str = Field(min_length=1)
    probs: dict[str, float]
    label: str = Field(min_length=1)
    age_bars: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> RegimeState:
        if not self.probs or any(not 0 <= p <= 1 for p in self.probs.values()):
            raise ValueError("regime probabilities must lie in [0, 1]")
        if abs(sum(self.probs.values()) - 1) > 1e-6:
            raise ValueError("regime probabilities must sum to one")
        if self.label not in self.probs:
            raise ValueError("the regime label must be one of the regimes")
        return self


class SignalCandidate(BaseModel):
    """A possible trade built from forecasts at ``ts`` (plan Phase 15), before the filters.

    Prices (``entry_ref``, ``stop``, ``target``) are absolute levels set in sigma-hat units at the
    decision; ``p_win`` is the calibrated probability used for EV (the conservative lower bound
    when the strategy asks for it, ``p_forecast`` the forecast's own), ``payoff_ratio`` the
    take-profit over the stop-loss distance, and ``ev_gross``, ``ev_costs`` and ``ev_net`` the EV
    terms in sigma-hat units over the horizon. ``sigma_hat`` is that horizon sigma-hat (a fraction
    of price).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    ts: AwareDatetime
    instrument: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    direction: Literal["long", "short"]
    entry_ref: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    horizon: timedelta
    p_forecast: float = Field(ge=0, le=1)
    p_win: float = Field(ge=0, le=1)
    payoff_ratio: float = Field(gt=0)
    ev_gross: float
    ev_costs: float = Field(ge=0)
    ev_net: float
    sigma_hat: float = Field(gt=0)
    regime: RegimeState | None = None
    forecast_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> SignalCandidate:
        below, above = (self.stop, self.target)
        if self.direction == "short":
            below, above = above, below
        if not below < self.entry_ref < above:
            raise ValueError(
                f"a {self.direction} candidate's stop and target are on the wrong sides"
            )
        return self


SignalOutcome = Literal["intent", "rejected", "not selected"]


class SignalRecord(BaseModel):
    """The audit record of one candidate, kept whatever happened to it (plan Phase 15).

    It holds the timestamp, instrument, direction, entry, stop, target, expected return (EV and the
    forecasts' expected return), probability, regime, volatility, risk/reward, model and feature
    versions — everything needed to explain the signal later — with every filter's outcome
    (``filters``: name -> ``pass`` or the blocking reason), the EV check, the ``outcome`` and its
    ``reasons``. A candidate that became a trade intent names it (``intent_id`` is the engine's
    stamp, filled when the runtime stamps the intent; ``signal_id`` of the intent is this
    ``record_id``). The size is the risk engine's: it is in the decision ledger, not here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str = Field(min_length=1)
    candidate: SignalCandidate
    forecasts: tuple[Forecast, ...] = Field(min_length=1)
    model_versions: dict[str, str]
    feature_set_versions: tuple[str, ...]
    sigma_daily: float | None = Field(default=None, gt=0)
    spread: float | None = Field(default=None, ge=0)
    filters: dict[str, str]
    ev_reasons: tuple[str, ...] = ()
    outcome: SignalOutcome
    reasons: tuple[str, ...] = ()
    intent: TradeIntent | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SignalRecord:
        if (self.outcome == "intent") != (self.intent is not None):
            raise ValueError("a record carries an intent exactly when its outcome is an intent")
        if self.outcome != "intent" and not self.reasons:
            raise ValueError("a candidate that did not become an intent must give its reasons")
        return self


#: The schemas exported to ``docs/specs/interfaces/`` (file stem -> model), in pipeline order.
INTERFACES: dict[str, type[BaseModel]] = {
    "forecast": Forecast,
    "regime_state": RegimeState,
    "signal_candidate": SignalCandidate,
    "signal_record": SignalRecord,
    "trade_intent": TradeIntent,
    "risk_decision": RiskDecision,
    "order_intent": OrderIntent,
}


def json_schemas() -> dict[str, str]:
    """Each interface's JSON Schema (validation mode) as canonical, indented JSON text."""
    return {
        name: json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        for name, model in INTERFACES.items()
    }


def write_json_schemas(directory: Path) -> list[Path]:
    """Write ``<name>.schema.json`` for every interface into `directory`; return the paths."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in json_schemas().items():
        path = directory / f"{name}.schema.json"
        path.write_text(text)
        paths.append(path)
    return paths


if __name__ == "__main__":  # uv run python -m xq.signals.schema docs/specs/interfaces
    for written in write_json_schemas(Path(sys.argv[1] if len(sys.argv) > 1 else ".")):
        print(written)
