"""BT-007: the decision ledger and the event backtest result — a golden hand-computed run, every
order linked to an approved decision of the risk engine, broken links detected, the risk
profile's version on every decision, Parquet round trip and determinism."""

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from helpers.event_backtest import (
    CAPITAL,
    CLOCK,
    RISK,
    SIGMA,
    RandomStrategy,
    ScriptedStrategy,
    exact_costs,
    random_quotes,
)
from helpers.pipeline import REPO
from xq.backtest.engine import EventBacktestResult, MarketData, run_event_backtest
from xq.backtest.ledger import Ledger
from xq.backtest.metrics import performance_metrics
from xq.core.types import Timeframe
from xq.signals.schema import TradeIntent

GOLDEN = REPO / "tests" / "fixtures" / "golden_trades" / "bracket_target_then_short.yaml"
ID_COLUMN = {
    "intent": "intent_id",
    "decision": "decision_id",
    "order": "order_id",
    "fill": "fill_id",
    "order_cancelled": "order_id",
}


def golden() -> tuple[dict[str, Any], EventBacktestResult]:
    case = yaml.safe_load(GOLDEN.read_text())
    q = pd.DataFrame(case["quotes"], columns=["ts_utc", "bid", "ask"])
    q["ts_utc"] = pd.to_datetime(q["ts_utc"], utc=True, format="ISO8601")
    script = {t: [TradeIntent(**intent)] for t, intent in case["intents"].items()}
    result = run_event_backtest(
        ScriptedStrategy(script),
        MarketData.from_ticks(q, Timeframe.M15),
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
        risk=RISK,
        sigma_daily=SIGMA,
    )
    return case["expected"], result


def test_a_golden_run_matches_the_hand_computation() -> None:
    expected, result = golden()
    fills = result.fills
    assert fills["fill_time"].tolist() == [
        pd.Timestamp(t, tz="UTC") for t, _, _, _ in expected["fills"]
    ]
    np.testing.assert_allclose(fills["lots"], [lots for _, lots, _, _ in expected["fills"]])
    np.testing.assert_allclose(fills["price"], [p for _, _, p, _ in expected["fills"]], rtol=1e-12)
    assert fills["role"].tolist() == [role for _, _, _, role in expected["fills"]]
    trades = result.trades
    assert not trades["open"].any()
    for column, n in (("side", 0), ("lots", 1), ("price_pnl", 2), ("commission", 3), ("pnl", 4)):
        np.testing.assert_allclose(trades[column], [t[n] for t in expected["trades"]], atol=1e-9)
    [day] = result.daily.itertuples()
    costs = expected["costs"]
    assert day.spread_cost == pytest.approx(costs["spread"])
    assert day.slippage_cost == pytest.approx(costs["slippage"])
    assert day.commission == pytest.approx(costs["commission"])
    assert day.financing == costs["financing"]
    assert day.gross_pnl == pytest.approx(expected["gross_pnl"])
    assert day.equity == pytest.approx(expected["final_equity"])
    rows = result.ledger
    chain = [
        [kind, row[ID_COLUMN[kind]]]
        for kind, row in zip(rows["kind"], rows.to_dict("records"), strict=True)
        if kind != "account"  # the risk state's equity observations (RISK-001)
    ]
    assert chain == expected["ledger"]
    assert result.link_problems == ()
    assert result.cost_basis == "screening, placeholder costs"
    assert result.risk_label == f"RiskEngine {RISK.config_version} (PROVISIONAL risk profile)"
    metrics = performance_metrics(result, 252)  # the BT-003 metrics apply to the event tier
    assert metrics["trade_count"] == 2
    assert metrics["net_profit"] == pytest.approx(expected["final_equity"] - CAPITAL)
    assert metrics["gross_profit"] - metrics["total_costs"] == pytest.approx(metrics["net_profit"])


def random_run(seed: int) -> EventBacktestResult:
    q = random_quotes("2024-03-11 00:00", "2024-03-16 00:00", seed=seed, every_s=30)
    return run_event_backtest(
        RandomStrategy(seed=seed, trade_probability=0.2),
        MarketData.from_ticks(q, Timeframe.M15),
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
        risk=RISK,
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_order_is_linked_to_an_approved_risk_decision(seed: int) -> None:
    result = random_run(seed)
    ledger = result.ledger
    assert result.link_problems == ()
    decisions = ledger.loc[ledger["kind"] == "decision"].set_index("decision_id")
    orders = ledger.loc[ledger["kind"] == "order"]
    assert len(orders) > 20
    assert orders["decision_id"].notna().all()
    assert decisions.loc[orders["decision_id"], "approved"].astype(bool).all()
    legs = orders.loc[orders["parent_order_id"].notna()]
    assert len(legs) > 0  # bracket legs carry their parent's decision
    assert (legs["decision_id"] == "D-" + legs["intent_id"]).all()
    fills = ledger.loc[ledger["kind"] == "fill"]
    assert set(fills["order_id"]) <= set(orders["order_id"])
    assert len(fills) == len(result.fills)
    # every decision is the risk engine's and names the profile it applied
    config = decisions["detail"].map(lambda d: yaml.safe_load(d)["config_version"])
    assert (config == RISK.config_version).all()
    assert RISK.config_version.startswith("risk-1@")
    # the interim sigma-hat needs 20 signal bars: entries before it are refused, and said so
    rejected = decisions.loc[~decisions["approved"].astype(bool), "reason"]
    assert rejected.str.contains("no sigma-hat").any()
    # refusals (decisions at the close) are recorded with their reason and no decision
    refusals = ledger.loc[ledger["kind"] == "refusal"]
    assert refusals["reason"].str.startswith("market closed").all()
    assert not set(refusals["intent_id"]) & set(decisions["intent_id"])
    summary = result.ledger_summary
    assert summary["rows"].sum() == len(ledger)
    assert summary.loc[summary["kind"] == "order", "rows"].sum() == len(orders)


def test_broken_links_are_reported() -> None:
    _, result = golden()
    ledger = Ledger()
    ledger.rows = [dict(row) for row in result.ledger.to_dict("records")]
    for row in ledger.rows:
        row["ts"] = pd.Timestamp(row["ts"]).value
    assert ledger.check_links() == []
    first = {
        kind: next(r for r in ledger.rows if r["kind"] == kind)
        for kind in ("decision", "order", "fill")
    }
    orphan = dict(first["order"], seq=99, order_id="O-X", decision_id="D-X")  # no decision
    rejected = {**first["decision"], "seq": 100, "decision_id": "D-R", "approved": False}
    backed = dict(first["order"], seq=101, order_id="O-R", decision_id="D-R")
    stray = dict(first["fill"], seq=102, order_id="O-missing")  # a fill without an order
    ledger.rows += [orphan, rejected, backed, stray]
    problems = ledger.check_links()
    assert "row 99 (order): order O-X has no risk decision" in problems
    assert "row 101 (order): order O-R has a rejected decision" in problems
    assert "row 102 (fill): order O-missing is not in the ledger" in problems


def test_the_ledger_round_trips_through_parquet(tmp_path: Path) -> None:
    result = random_run(1)
    ledger = Ledger()
    ledger.rows = [dict(row) for row in result.ledger.to_dict("records")]
    for row in ledger.rows:
        row["ts"] = pd.Timestamp(row["ts"]).value
    paths = ledger.write(tmp_path)
    back = pd.read_parquet(paths["ledger"])
    pd.testing.assert_frame_equal(back, result.ledger)
    summary = pd.read_parquet(paths["summary"])
    pd.testing.assert_frame_equal(summary, result.ledger_summary)


def test_event_backtests_are_deterministic() -> None:
    first, second = random_run(2), random_run(2)
    pd.testing.assert_frame_equal(first.ledger, second.ledger)
    pd.testing.assert_frame_equal(first.fills, second.fills)
    pd.testing.assert_frame_equal(first.daily, second.daily)
    pd.testing.assert_frame_equal(first.trades, second.trades)
    assert first.events == second.events
    assert first.ambiguity == second.ambiguity
