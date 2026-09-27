"""SIGNAL-005: a candidate strategy runs forecast to fill in the event backtester — forecasts,
signal engine, risk engine, broker — and every fill traces back through its order, risk decision,
intent and signal record to the calibrated forecasts behind it; uncalibrated forecasts never
reach the risk engine."""

import json
import math
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CLOCK,
    RISK,
    SESSIONS,
    exact_costs,
    random_quotes,
)
from helpers.pipeline import REPO
from xq.backtest.engine import EventBacktestResult, MarketData, StrategyContext, run_event_backtest
from xq.backtest.events import Bar
from xq.backtest.strategies import SignalStrategy
from xq.core.time import from_ns
from xq.core.types import Timeframe
from xq.signals.engine import UNCALIBRATED, SignalEngine, load_strategy_spec
from xq.signals.schema import Forecast

SPEC = load_strategy_spec(REPO / "experiments" / "configs" / "strategies" / "template_barrier.yaml")
HORIZON_DAYS = 4 / 23  # four hours of a 23-hour trading day


class SyntheticMomentum:
    """A causal test forecaster, NOT a model: the take-profit-first probability of each side
    leans with the last eight signal-bar returns. Its forecasts are *declared* calibrated so the
    engine can be exercised; nothing here is evidence about any strategy."""

    def __init__(self, *, calibrated: bool = True) -> None:
        self.calibrated = calibrated
        self.closes: list[float] = []

    def __call__(self, bar: Bar, ctx: StrategyContext) -> list[Forecast]:
        self.closes.append(bar.close)
        if len(self.closes) < 9 or ctx.sigma_daily is None:
            return []
        sigma = ctx.sigma_daily * math.sqrt(HORIZON_DAYS)
        z = math.log(self.closes[-1] / self.closes[-9]) / sigma
        lean = 0.1 * math.tanh(z)
        return [
            Forecast(
                forecast_id=f"F-{side}-{ctx.now}",
                ts=from_ns(ctx.now),
                instrument="xauusd",
                horizon=timedelta(hours=4),
                model_id="synthetic_momentum",
                model_version="test",
                feature_set_version="closes-8",
                target_id="barrier.4h.tp2.sl1",
                side=side,
                p_tp_first=0.45 + (lean if side == "long" else -lean),
                p_se=0.05,
                sigma_hat=sigma,
                calibrated=self.calibrated,
                calibration_id="declared-for-the-test" if self.calibrated else None,
            )
            for side in ("long", "short")
        ]


def run(calibrated: bool = True) -> tuple[SignalStrategy, EventBacktestResult]:
    strategy = SignalStrategy(
        SignalEngine(SPEC, exact_costs(), SESSIONS), SyntheticMomentum(calibrated=calibrated)
    )
    q = random_quotes("2024-03-11 00:00", "2024-03-16 00:00", seed=7, every_s=20, step=0.2)
    result = run_event_backtest(
        strategy,
        MarketData.from_ticks(q, Timeframe.M15),
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
        risk=RISK,
    )
    return strategy, result


@pytest.fixture(scope="module")
def calibrated_run() -> tuple[SignalStrategy, EventBacktestResult]:
    return run()


def test_every_fill_traces_back_to_calibrated_forecasts(
    calibrated_run: tuple[SignalStrategy, EventBacktestResult],
) -> None:
    strategy, result = calibrated_run
    assert result.link_problems == ()
    audit = strategy.audit(result.ledger)
    assert len(audit) == len(result.fills) > 10
    assert audit["order_known"].all()
    assert audit["approved"].all()
    assert (audit["config_version"] == RISK.config_version).all()
    assert {"entry", "stop_loss", "exit"} <= set(audit["role"])  # stops and time-stop exits
    # a fill comes from a signal's intent, or from the engine's time stop of one
    assert set(audit["intent_reason"]) <= {"", "time stop"}
    signals = audit.loc[audit["intent_reason"] == ""]
    assert signals["signal_id"].notna().all()
    assert (signals["record_outcome"] == "intent").all()
    assert signals["forecast_ids"].map(len).gt(0).all()
    assert signals["forecasts_known"].all()
    assert signals["calibrated"].all()


def test_every_candidate_is_recorded_and_every_intent_is_its_records(
    calibrated_run: tuple[SignalStrategy, EventBacktestResult],
) -> None:
    strategy, result = calibrated_run
    records = {r.record_id: r for r in strategy.records}
    assert len(strategy.forecasts) > 100
    assert len(records) == len(strategy.forecasts)  # one record per candidate, rejected or not
    outcomes = pd.Series([r.outcome for r in strategy.records]).value_counts()
    assert {"intent", "rejected", "not selected"} <= set(outcomes.index)
    ledger = result.ledger
    intents = ledger.loc[(ledger["kind"] == "intent") & (ledger["reason"] == "")]
    details = intents["detail"].map(json.loads)
    assert {d["signal_id"] for d in details} == {
        r.record_id for r in strategy.records if r.outcome == "intent"
    }
    for (_, row), detail in zip(intents.iterrows(), details, strict=True):
        record = records[detail["signal_id"]]
        assert record.intent is not None
        assert (detail["stop"], detail["target"]) == (record.intent.stop, record.intent.target)
        assert record.candidate.ts == pd.Timestamp(row["ts"])  # decided when forecast
        assert all(f.ts == record.candidate.ts for f in record.forecasts)
    # the default profile sizes these 2:1 trades on their edge per unit of risk (ADR 0053)
    decisions = ledger.loc[ledger["kind"] == "decision"]
    signal_decisions = decisions.loc[decisions["intent_id"].isin(intents["intent_id"])]
    snapshots = signal_decisions["detail"].map(lambda d: json.loads(d)["limits_snapshot"])
    edges = snapshots.map(lambda s: s.get("ev_r")).dropna().to_numpy(np.float64)
    scales = snapshots.map(lambda s: s.get("edge_scale")).dropna().to_numpy(np.float64)
    assert len(edges) > 0
    assert (scales > 0).any()
    assert ((scales >= 0) & (scales <= 1)).all()
    np.testing.assert_allclose(scales, np.clip(edges / RISK.config.sizing.ev_r_full, 0, 1))


def test_uncalibrated_forecasts_never_reach_the_risk_engine() -> None:
    strategy, result = run(calibrated=False)
    assert len(strategy.records) > 100
    assert all(r.outcome == "rejected" for r in strategy.records)
    assert all(r.reasons[0] == UNCALIBRATED for r in strategy.records)
    assert (result.ledger["kind"] == "intent").sum() == 0
    assert result.fills.empty
