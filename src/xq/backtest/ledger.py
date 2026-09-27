"""The decision ledger of the event tier (BT-007).

Every step of the decision chain is one row, in the order it happened, with the ids that link
it to the rest of the chain:

- ``intent`` — a strategy (or engine: time stop, weekend exit) intent, with its decision time;
- ``refusal`` — an intent refused before the risk decision (market closed, entry blackout),
  with the reason;
- ``decision`` — the risk decision on an intent: approved or rejected, the size, the reasons and
  the approver's configuration version (the Sprint 11 placeholder says so in both);
- ``order`` — an order sent to the broker (``decision_id`` and ``intent_id`` of the decision that
  approved it) or a bracket leg placed when its parent filled (``parent_order_id``; it carries
  the parent's decision, which approved its stop and target);
- ``order_rejected`` / ``order_cancelled`` / ``order_expired`` — what happened to an order that
  did not fill, with the reason;
- ``fill`` — an execution, with its order, decision and intent;
- ``account`` — the account the risk state observed, at every risk decision and at every trading
  day's end, so the risk state can be rebuilt from the ledger (RISK-001).

`Ledger.check_links` lists every row whose links are broken — above all, every order that is not
backed by an approved risk decision — so a test (and any later audit) can require that list to be
empty. `Ledger.write` stores the ledger as Parquet with a summary (row counts by kind and
reason) next to it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from xq.backtest.events import AccountState, Fill
from xq.core.time import from_ns, to_ns
from xq.core.types import Side
from xq.signals.schema import OrderIntent, RiskDecision, TradeIntent

LEDGER_FILE = "ledger.parquet"
SUMMARY_FILE = "ledger_summary.parquet"
LEDGER_COLUMNS = (
    "seq",
    "ts",
    "kind",
    "intent_id",
    "decision_id",
    "order_id",
    "parent_order_id",
    "fill_id",
    "strategy_id",
    "side",
    "lots",
    "price",
    "order_type",
    "role",
    "approved",
    "reason",
    "detail",
)
KINDS = (
    "intent",
    "refusal",
    "decision",
    "order",
    "order_rejected",
    "order_cancelled",
    "order_expired",
    "fill",
    "account",
)


class Ledger:
    """Append-only record of the decision chain (module docstring)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._orders: dict[str, tuple[str, str]] = {}
        self._strategies: dict[str, str] = {}

    # the engine's side --------------------------------------------------------------------------

    def intent(self, intent: TradeIntent) -> None:
        """A decided intent (stamped with its id and decision time)."""
        intent_id = _required(intent.intent_id, "intent_id")
        self._strategies[intent_id] = intent.strategy_id
        self._add(
            _instant(intent.created_at),
            "intent",
            intent_id=intent_id,
            side=intent.direction,
            lots=None,
            price=intent.limit_price,
            order_type=intent.entry_type,
            reason=intent.reason,
            detail={
                "exposure": intent.exposure,
                "stop": intent.stop,
                "target": intent.target,
                "time_stop": None if intent.time_stop is None else str(intent.time_stop),
                "signal_id": intent.signal_id,
            },
        )

    def refusal(self, intent: TradeIntent, reason: str) -> None:
        """An intent refused before the risk decision."""
        self._add(
            _instant(intent.created_at),
            "refusal",
            intent_id=_required(intent.intent_id, "intent_id"),
            reason=reason,
        )

    def decision(self, decision: RiskDecision) -> None:
        """A risk decision."""
        self._add(
            _instant(decision.decided_at),
            "decision",
            intent_id=decision.intent_id,
            decision_id=decision.decision_id,
            side=None if decision.side is None else decision.side.value,
            lots=decision.size_lots,
            approved=decision.approved,
            reason="; ".join(decision.reasons),
            detail={
                "target_lots": decision.target_lots,
                "adjusted_stop": decision.adjusted_stop,
                "target": decision.target,
                "limits_snapshot": decision.limits_snapshot,
                "config_version": decision.config_version,
            },
        )

    def order(self, order: OrderIntent, *, submitted_at: int, arrival: int | None) -> None:
        """An order sent to the broker."""
        self._orders[order.order_id] = (order.decision_id, order.intent_id)
        self._add(
            submitted_at,
            "order",
            intent_id=order.intent_id,
            decision_id=order.decision_id,
            order_id=order.order_id,
            side=order.side.value,
            lots=order.size_lots,
            price=order.price,
            order_type=order.order_type,
            detail={
                "stop": order.stop,
                "target": order.target,
                "time_stop": None if order.time_stop is None else str(order.time_stop),
                "expected_position_lots": order.expected_position_lots,
                "arrival": None if arrival is None else str(from_ns(arrival)),
                "idempotency_key": order.idempotency_key,
            },
        )

    def fill(self, fill: Fill) -> None:
        """A booked fill."""
        self._add(
            fill.ts,
            "fill",
            intent_id=fill.intent_id,
            decision_id=fill.decision_id,
            order_id=fill.order_id,
            fill_id=fill.fill_id,
            side="buy" if fill.lots > 0 else "sell",
            lots=abs(fill.lots),
            price=fill.price,
            order_type=fill.order_type,
            role=fill.role,
            detail={
                "bid": fill.bid,
                "ask": fill.ask,
                "mid": fill.mid,
                "slippage_bps": fill.slippage_bps,
                "spread_cost": fill.spread_cost,
                "slippage_cost": fill.slippage_cost,
                "commission": fill.commission,
                "position_after": fill.position_after,
            },
        )

    def account(self, account: AccountState, event: str) -> None:
        """The account the risk state observed (``event``: ``decision`` or ``day_end``)."""
        self._add(
            account.ts,
            "account",
            lots=account.position_lots,
            reason=event,
            detail={
                "event": event,
                "capital": account.capital,
                "cash": account.cash,
                "unrealized": account.unrealized,
                "equity": account.equity,
                "position_lots": account.position_lots,
                "margin_used": account.margin_used,
                "mark": account.mark if math.isfinite(account.mark) else None,
            },
        )

    # the broker's side --------------------------------------------------------------------------

    def bracket_order(
        self,
        *,
        order_id: str,
        parent_order_id: str,
        decision_id: str,
        intent_id: str,
        ts: int,
        side: Side,
        lots: float,
        order_type: str,
        price: float,
        role: str,
    ) -> None:
        """A bracket leg placed when its parent filled."""
        self._orders[order_id] = (decision_id, intent_id)
        self._add(
            ts,
            "order",
            intent_id=intent_id,
            decision_id=decision_id,
            order_id=order_id,
            parent_order_id=parent_order_id,
            side=side.value,
            lots=lots,
            price=price,
            order_type=order_type,
            role=role,
        )

    def order_rejected(self, order_id: str, ts: int, reason: str) -> None:
        """An order refused on arrival."""
        self._order_event("order_rejected", order_id, ts, reason)

    def order_cancelled(self, order_id: str, ts: int, reason: str) -> None:
        """A working order cancelled."""
        self._order_event("order_cancelled", order_id, ts, reason)

    def order_expired(self, order_id: str, ts: int, reason: str) -> None:
        """An order expired unfilled."""
        self._order_event("order_expired", order_id, ts, reason)

    # reading ------------------------------------------------------------------------------------

    def frame(self) -> pd.DataFrame:
        """The ledger as a frame (``ts`` tz-aware UTC; ``detail`` as JSON text)."""
        frame = pd.DataFrame(self.rows, columns=list(LEDGER_COLUMNS))
        frame["ts"] = pd.to_datetime(frame["ts"], unit="ns", utc=True)
        frame["seq"] = frame["seq"].astype("int64")
        frame["lots"] = frame["lots"].astype("float64")
        frame["price"] = frame["price"].astype("float64")
        return frame

    def summary(self) -> pd.DataFrame:
        """Row counts by kind and reason (the reason is empty where it carries none)."""
        frame = self.frame()
        reasons = frame["reason"].fillna("")
        keep = frame["kind"].isin(["refusal", "order_rejected", "order_cancelled", "order_expired"])
        reasons = reasons.where(keep, "")
        counts = (
            pd.DataFrame({"kind": frame["kind"], "reason": reasons})
            .groupby(["kind", "reason"], sort=False)
            .size()
            .rename("rows")
            .reset_index()
        )
        order = {kind: n for n, kind in enumerate(KINDS)}
        counts["_order"] = counts["kind"].map(order)
        counts = counts.sort_values(["_order", "reason"], kind="stable").drop(columns="_order")
        return counts.reset_index(drop=True)

    def check_links(self) -> list[str]:
        """Every broken link in the ledger (empty when every chain is complete)."""
        problems: list[str] = []
        intents = {r["intent_id"] for r in self.rows if r["kind"] == "intent"}
        decisions = {r["decision_id"]: r for r in self.rows if r["kind"] == "decision"}
        orders = {r["order_id"]: r for r in self.rows if r["kind"] == "order"}
        for row in self.rows:
            kind = str(row["kind"])
            where = f"row {row['seq']} ({kind})"
            if kind in ("refusal", "decision") and row["intent_id"] not in intents:
                problems.append(f"{where}: intent {row['intent_id']} is not in the ledger")
            if kind == "order":
                decision = decisions.get(row["decision_id"])
                if decision is None:
                    problems.append(f"{where}: order {row['order_id']} has no risk decision")
                    continue
                if not decision["approved"]:
                    problems.append(f"{where}: order {row['order_id']} has a rejected decision")
                if decision["intent_id"] != row["intent_id"]:
                    problems.append(f"{where}: order {row['order_id']} and its decision disagree")
                parent = row["parent_order_id"]
                if parent is not None and parent not in orders:
                    problems.append(f"{where}: parent order {parent} is missing")
            if kind == "fill" or kind.startswith("order_"):
                order = orders.get(row["order_id"])
                if order is None:
                    problems.append(f"{where}: order {row['order_id']} is not in the ledger")
                elif order["decision_id"] != row["decision_id"]:
                    problems.append(f"{where}: {row['order_id']} carries another decision")
        return problems

    def write(self, directory: Path) -> dict[str, Path]:
        """Write ``ledger.parquet`` and ``ledger_summary.parquet`` into `directory`."""
        directory.mkdir(parents=True, exist_ok=True)
        paths = {"ledger": directory / LEDGER_FILE, "summary": directory / SUMMARY_FILE}
        self.frame().to_parquet(paths["ledger"], index=False)
        self.summary().to_parquet(paths["summary"], index=False)
        return paths

    # internals ----------------------------------------------------------------------------------

    def _order_event(self, kind: str, order_id: str, ts: int, reason: str) -> None:
        decision_id, intent_id = self._orders.get(order_id, (None, None))
        self._add(
            ts,
            kind,
            intent_id=intent_id,
            decision_id=decision_id,
            order_id=order_id,
            reason=reason,
        )

    def _add(self, ts: int, kind: str, **fields: Any) -> None:
        detail = fields.pop("detail", None)
        intent_id = fields.get("intent_id")
        row: dict[str, Any] = dict.fromkeys(LEDGER_COLUMNS)
        row.update(fields)
        row.update(
            {
                "seq": len(self.rows),
                "ts": int(ts),
                "kind": kind,
                "strategy_id": self._strategies.get(intent_id) if intent_id else None,
                "detail": None
                if detail is None
                else json.dumps(detail, sort_keys=True, default=str),
            }
        )
        self.rows.append(row)


def _required(value: str | None, name: str) -> str:
    if value is None:
        raise ValueError(f"the engine stamps {name} before the ledger records the intent")
    return value


def _instant(value: Any) -> int:
    if value is None:
        raise ValueError("the engine stamps created_at before the ledger records the intent")
    return to_ns(pd.Timestamp(value))
