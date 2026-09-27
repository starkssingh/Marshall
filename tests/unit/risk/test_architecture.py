"""RISK-005: no `OrderIntent` without an approved decision of the risk engine.

Two layers enforce the plan's construction rule:

- **at runtime**, `OrderIntent` refuses a decision that is not approved or was not issued by the
  risk engine (built by hand, validated from a dict, or copied), and the ways around validation
  (`model_construct`, `model_copy` with changes) are disabled;
- **statically**, this test parses every module under ``src/`` and fails if any builds an
  `OrderIntent` other than through `OrderIntent.from_decision`, builds or issues a
  `RiskDecision` outside the risk engine, or sets the issued mark. Planted violations show that
  the check catches each form, including aliased imports.
"""

import ast
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.event_backtest import RISK, market_state, risk_state
from helpers.pipeline import REPO
from xq.core.types import Side
from xq.risk.engine import issue_decision
from xq.signals.schema import OrderIntent, RiskDecision, TradeIntent

SRC = REPO / "src"
ISSUER = Path("xq/risk/engine.py")  # the only module that builds and issues decisions
SCHEMA = Path("xq/signals/schema.py")  # defines the mark and OrderIntent.from_decision
GUARDED = {"OrderIntent", "RiskDecision", "issue_decision"}
BYPASSES = {"model_construct", "model_validate", "model_validate_json", "model_copy"}


def violations(module: Path, source: str) -> list[str]:
    """Every place in `source` (module `module`, relative to ``src``) that breaks the rule."""
    tree = ast.parse(source)
    names = {name: name for name in GUARDED}  # local name -> guarded name (aliases included)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in GUARDED:
                    names[alias.asname or alias.name] = alias.name

    def guarded(expr: ast.expr) -> str | None:
        """The guarded name `expr` refers to: a (possibly aliased) name or ``module.Name``."""
        if isinstance(expr, ast.Name):
            return names.get(expr.id)
        if isinstance(expr, ast.Attribute) and expr.attr in GUARDED:
            return expr.attr
        return None

    found: list[str] = []

    def flag(node: ast.AST, what: str) -> None:
        found.append(f"{module}:{getattr(node, 'lineno', 0)}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = guarded(func)
            if called == "OrderIntent":
                flag(node, "builds an OrderIntent other than through OrderIntent.from_decision")
            elif called is not None and module != ISSUER:
                flag(node, f"calls {called} outside the risk engine")
            if (
                isinstance(func, ast.Attribute)
                and func.attr in BYPASSES
                and guarded(func.value) == "OrderIntent"
            ):
                flag(node, f"OrderIntent.{func.attr} bypasses OrderIntent.from_decision")
            sets = (isinstance(func, ast.Name) and func.id == "setattr") or (
                isinstance(func, ast.Attribute) and func.attr == "__setattr__"
            )
            if sets and any(
                isinstance(a, ast.Constant) and a.value == "_issued" for a in node.args
            ):
                flag(node, "sets the issued mark by setattr")
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "_issued"
                and module not in (ISSUER, SCHEMA)
            ):
                flag(node, "sets the issued mark outside the risk engine")
    return found


def test_no_module_under_src_builds_an_order_without_the_risk_engine() -> None:
    modules = sorted(SRC.rglob("*.py"))
    assert len(modules) > 50
    problems = [
        problem
        for path in modules
        for problem in violations(path.relative_to(SRC), path.read_text())
    ]
    assert problems == []


PLANTED = {
    "direct": "from xq.signals.schema import OrderIntent\nOrderIntent(decision=d)\n",
    "aliased": "from xq.signals.schema import OrderIntent as Order\nOrder(decision=d)\n",
    "module attribute": "import xq.signals.schema as s\ns.OrderIntent(decision=d)\n",
    "module construct": "import xq.signals.schema as s\ns.OrderIntent.model_construct()\n",
    "construct": "from xq.signals.schema import OrderIntent\nOrderIntent.model_construct()\n",
    "validate": "from xq.signals.schema import OrderIntent\nOrderIntent.model_validate({})\n",
    "hand-made decision": "from xq.signals.schema import RiskDecision\nRiskDecision(x=1)\n",
    "issued elsewhere": "from xq.risk.engine import issue_decision\nissue_decision(x=1)\n",
    "issued via module": "import xq.risk.engine as e\ne.issue_decision(approved=True)\n",
    "marked": "d._issued = True\n",
    "marked by setattr": "object.__setattr__(d, '_issued', True)\n",
}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_the_check_catches_a_planted_violation(case: str) -> None:
    assert violations(Path("xq/backtest/rogue.py"), PLANTED[case])


def test_the_risk_engine_itself_may_issue_decisions() -> None:
    source = (REPO / "src" / ISSUER).read_text()
    assert "issue_decision(" in source
    assert violations(ISSUER, source) == []


# --- runtime --------------------------------------------------------------------------------------


def intent() -> TradeIntent:
    return TradeIntent(direction="long", exposure=1.0, stop=1990.1).model_copy(
        update={"intent_id": "I000001", "created_at": pd.Timestamp("2024-03-12 14:00", tz="UTC")}
    )


def order_fields(decision: RiskDecision) -> dict[str, object]:
    return {
        "decision": decision,
        "order_id": "O-I000001",
        "side": Side.BUY,
        "size_lots": decision.size_lots,
        "order_type": "market",
        "stop": decision.adjusted_stop,
        "target": decision.target,
        "expected_position_lots": 0.0,
        "idempotency_key": "O-I000001",
    }


def test_an_order_needs_an_approved_decision_the_risk_engine_issued() -> None:
    decision = RISK.evaluate(intent(), risk_state(), market_state())
    assert decision.issued
    order = OrderIntent.from_decision(decision, intent(), expected_position_lots=0.0)
    assert order.decision is decision
    assert OrderIntent(**order_fields(decision)) == order  # type: ignore[arg-type]
    # the same decision built by hand is not issued
    forged = RiskDecision(**decision.model_dump())
    assert not forged.issued
    with pytest.raises(ValidationError, match="risk engine issued"):
        OrderIntent(**order_fields(forged))  # type: ignore[arg-type]
    # nor is a copy, even an unchanged one, nor a decision revalidated from its data
    for copy in (decision.model_copy(), decision.model_copy(update={"size_lots": 5.0})):
        assert not copy.issued
        with pytest.raises(ValidationError, match="risk engine issued"):
            OrderIntent(**order_fields(copy))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="risk engine issued"):
        OrderIntent.model_validate(order.model_dump())
    # a rejected decision, issued or not, backs no order
    rejected = RISK.evaluate(intent().model_copy(update={"stop": None}), risk_state(), None)
    assert rejected.issued
    assert not rejected.approved
    with pytest.raises(ValidationError, match="approved risk decision"):
        OrderIntent(**{**order_fields(decision), "decision": rejected})  # type: ignore[arg-type]


def test_the_ways_around_validation_are_closed() -> None:
    decision = RISK.evaluate(intent(), risk_state(), market_state())
    order = OrderIntent.from_decision(decision, intent(), expected_position_lots=0.0)
    with pytest.raises(TypeError, match="model_construct"):
        OrderIntent.model_construct(**order_fields(decision))
    with pytest.raises(TypeError, match="cannot be changed"):
        order.model_copy(update={"size_lots": 50.0})
    assert order.model_copy() == order  # an unchanged copy is the same order
    with pytest.raises(ValidationError):
        order.size_lots = 50.0  # type: ignore[misc]  # frozen


def test_issue_decision_validates_like_the_schema() -> None:
    with pytest.raises(ValidationError, match="rejected decision"):
        issue_decision(
            decision_id="D-1",
            intent_id="I1",
            decided_at=pd.Timestamp("2024-03-12", tz="UTC"),
            approved=False,
            side=Side.BUY,
            size_lots=1.0,
            reasons=("x",),
            config_version="test",
        )
