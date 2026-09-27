"""SIGNAL-001: the decision-chain schemas — JSON round trips, validation, the JSON Schema files in
``docs/specs/interfaces/`` kept in sync, and an audit record complete by construction."""

from datetime import timedelta
from typing import Any

import pandas as pd
import pytest
from pydantic import BaseModel, ValidationError

from helpers.event_backtest import RISK, market_state, risk_state
from helpers.pipeline import REPO
from xq.signals.schema import (
    INTERFACES,
    Forecast,
    OrderIntent,
    RegimeState,
    RiskDecision,
    SignalCandidate,
    SignalRecord,
    TradeIntent,
    json_schemas,
)

TS = pd.Timestamp("2024-03-12 14:00", tz="UTC")
INTERFACE_DIR = REPO / "docs" / "specs" / "interfaces"


def forecast(**changes: Any) -> Forecast:
    fields: dict[str, Any] = {
        "forecast_id": "F-1",
        "ts": TS,
        "instrument": "xauusd",
        "horizon": timedelta(hours=4),
        "model_id": "synthetic",
        "model_version": "1",
        "feature_set_version": "fs-1",
        "target_id": "barrier.4h.tp2.sl1",
        "side": "long",
        "p_tp_first": 0.6,
        "p_se": 0.05,
        "quantiles": {0.1: -0.004, 0.5: 0.0005, 0.9: 0.005},
        "sigma_hat": 0.004,
        "calibrated": True,
        "calibration_id": "isotonic-fold-3",
    }
    fields.update(changes)
    return Forecast(**fields)


REGIME = RegimeState(
    ts=TS, model_version="hmm-1", probs={"trend": 0.7, "range": 0.3}, label="trend", age_bars=5
)


def candidate(**changes: Any) -> SignalCandidate:
    fields: dict[str, Any] = {
        "candidate_id": "S-1",
        "ts": TS,
        "instrument": "xauusd",
        "strategy_id": "barrier_example",
        "strategy_version": "1",
        "direction": "long",
        "entry_ref": 2000.1,
        "stop": 1992.1,
        "target": 2016.1,
        "horizon": timedelta(hours=4),
        "p_forecast": 0.6,
        "p_win": 0.51775,
        "payoff_ratio": 2.0,
        "ev_gross": 0.55325,
        "ev_costs": 0.2,
        "ev_net": 0.35325,
        "sigma_hat": 0.004,
        "regime": REGIME,
        "forecast_ids": ("F-1",),
    }
    fields.update(changes)
    return SignalCandidate(**fields)


def record(**changes: Any) -> SignalRecord:
    intent = TradeIntent(
        direction="long",
        exposure=1.0,
        stop=1992.1,
        target=2016.1,
        time_stop=TS + timedelta(hours=4),
        p_win=0.51775,
        calibrated=True,
        strategy_id="barrier_example",
        signal_id="S-1",
    )
    fields: dict[str, Any] = {
        "record_id": "S-1",
        "candidate": candidate(),
        "forecasts": (forecast(),),
        "model_versions": {"synthetic": "1"},
        "feature_set_versions": ("fs-1",),
        "sigma_daily": 0.0096,
        "spread": 0.2,
        "filters": {"regime": "pass", "session": "pass"},
        "outcome": "intent",
        "intent": intent,
    }
    fields.update(changes)
    return SignalRecord(**fields)


@pytest.mark.parametrize(
    "model",
    [forecast(), REGIME, candidate(), record(), record().intent],
    ids=["forecast", "regime", "candidate", "record", "intent"],
)
def test_the_signal_schemas_round_trip_through_json(model: BaseModel) -> None:
    text = model.model_dump_json()
    assert type(model).model_validate_json(text) == model
    assert type(model).model_validate(model.model_dump(mode="json")) == model


def test_decisions_serialize_but_only_the_risk_engine_issues_them() -> None:
    intent = TradeIntent(direction="long", exposure=1.0, stop=1990.1).model_copy(
        update={"intent_id": "I000001", "created_at": TS}
    )
    decision = RISK.evaluate(intent, risk_state(), market_state())
    back = RiskDecision.model_validate_json(decision.model_dump_json())
    assert back.model_dump() == decision.model_dump()
    assert not back.issued  # data is not a decision of the risk engine
    order = OrderIntent.from_decision(decision, intent, expected_position_lots=0.0)
    with pytest.raises(ValidationError, match="risk engine issued"):
        OrderIntent.model_validate_json(order.model_dump_json())


def test_forecasts_are_validated() -> None:
    with pytest.raises(ValidationError, match="needs p_up"):
        forecast(p_tp_first=None, quantiles=None, p_se=None)
    with pytest.raises(ValidationError, match="calibration_id"):
        forecast(calibration_id=None)
    assert not forecast(calibrated=False, calibration_id=None).calibrated
    with pytest.raises(ValidationError, match="quantile levels"):
        forecast(quantiles={1.5: 0.0})
    with pytest.raises(ValidationError, match="horizon must be positive"):
        forecast(horizon=timedelta(0))
    with pytest.raises(ValidationError):
        forecast(p_tp_first=1.2)
    with pytest.raises(ValidationError):
        forecast(ts=pd.Timestamp("2024-03-12 14:00"))  # naive timestamps are refused


def test_regime_states_are_validated() -> None:
    with pytest.raises(ValidationError, match="sum to one"):
        RegimeState(ts=TS, model_version="1", probs={"a": 0.5, "b": 0.4}, label="a", age_bars=0)
    with pytest.raises(ValidationError, match="one of the regimes"):
        RegimeState(ts=TS, model_version="1", probs={"a": 1.0}, label="b", age_bars=0)


def test_candidates_and_records_are_validated() -> None:
    with pytest.raises(ValidationError, match="wrong sides"):
        candidate(stop=2016.1, target=1992.1)
    with pytest.raises(ValidationError, match="exactly when"):
        record(outcome="rejected", reasons=("x",))  # a rejected record carries no intent
    with pytest.raises(ValidationError, match="give its reasons"):
        record(outcome="not selected", intent=None)
    rejected = record(outcome="rejected", intent=None, reasons=("uncalibrated forecast",))
    assert rejected.intent is None


def test_the_committed_json_schemas_are_current() -> None:
    expected = json_schemas()
    assert sorted(expected) == sorted(INTERFACES)
    for name, text in expected.items():
        path = INTERFACE_DIR / f"{name}.schema.json"
        assert path.exists(), (
            f"{path} is missing: uv run python -m xq.signals.schema {INTERFACE_DIR}"
        )
        assert path.read_text() == text, f"{path} is stale: regenerate it"


def test_a_signal_record_holds_every_audit_field_of_the_plan() -> None:
    # plan Phase 15: timestamp, instrument, direction, entry, stop, target, expected return,
    # probability, regime, volatility, risk/reward, model and feature versions (the size is the
    # risk engine's, in the decision ledger linked through the intent's signal_id)
    schema = SignalRecord.model_json_schema()
    candidate_props = schema["$defs"]["SignalCandidate"]["properties"]
    for field in ("ts", "instrument", "direction", "entry_ref", "stop", "target", "ev_net"):
        assert field in candidate_props
    for field in ("p_win", "p_forecast", "regime", "sigma_hat", "payoff_ratio", "forecast_ids"):
        assert field in candidate_props
    record_props = schema["properties"]
    for field in ("model_versions", "feature_set_versions", "forecasts", "filters", "outcome"):
        assert field in record_props
        assert field in schema["required"]
    assert "exp_return" in schema["$defs"]["Forecast"]["properties"]


def test_an_intents_probability_belongs_to_an_entry() -> None:
    with pytest.raises(ValidationError, match="flat intent"):
        TradeIntent(direction="flat", p_win=0.6)
    intent = TradeIntent(direction="short", exposure=0.5, stop=2010.0, p_win=0.6)
    assert not intent.calibrated  # a probability is uncalibrated unless declared otherwise
