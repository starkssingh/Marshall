"""SIGNAL-004: the signal engine — a strategy defined by its YAML; uncalibrated, stale and filtered
forecasts rejected with their reasons; stops, targets and EV in sigma units computed by hand;
every candidate recorded, at most one intent per decision."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from helpers.event_backtest import SESSIONS, exact_costs, ns
from helpers.pipeline import REPO
from xq.signals.engine import UNCALIBRATED, SignalEngine, StrategySpec, load_strategy_spec
from xq.signals.filters import REGIME_PLACEHOLDER
from xq.signals.schema import Forecast

TEMPLATE = REPO / "experiments" / "configs" / "strategies" / "template_barrier.yaml"
SPEC = load_strategy_spec(TEMPLATE)
NOW = "2024-03-12 14:00"


def forecast(side: str = "long", p: float = 0.6, **changes: Any) -> Forecast:
    fields: dict[str, Any] = {
        "forecast_id": f"F-{side}",
        "ts": pd.Timestamp(NOW, tz="UTC"),
        "instrument": "xauusd",
        "horizon": timedelta(hours=4),
        "model_id": "synthetic_momentum",
        "model_version": "1",
        "feature_set_version": "fs-1",
        "target_id": "barrier.4h.tp2.sl1",
        "side": side,
        "p_tp_first": p,
        "p_se": 0.05,
        "sigma_hat": 0.004,
        "calibrated": True,
        "calibration_id": "isotonic",
    }
    fields.update(changes)
    return Forecast(**fields)


def engine(spec: StrategySpec = SPEC) -> SignalEngine:
    signals = SignalEngine(spec, exact_costs(), SESSIONS)
    for day in range(1, 9):  # a spread history: the reference needs 8 spreads (overall median)
        signals.observe_spread(ns(f"2024-03-0{day} 03:00"), 0.2)
    return signals


def decide(signals: SignalEngine, *forecasts: Forecast, at: str = NOW, **kwargs: Any) -> Any:
    return signals.decide(
        ns(at), list(forecasts), bid=1999.9, ask=2000.1, sigma_daily=0.0096, **kwargs
    )


def test_the_template_strategy_is_defined_entirely_by_its_yaml() -> None:
    assert SPEC.strategy_id == "template_barrier"
    assert SPEC.horizon == timedelta(hours=4)
    assert (SPEC.barriers.tp_sigmas, SPEC.barriers.sl_sigmas) == (2.0, 1.0)
    assert SPEC.ev.conservative is not None
    names = [f.name for f in engine().filters]
    assert names == ["regime", "blackout", "volatility", "spread"]
    with pytest.raises(ValidationError):
        StrategySpec.model_validate({**yaml.safe_load(TEMPLATE.read_text()), "size": 3})


def test_a_qualifying_forecast_becomes_one_intent_in_sigma_units() -> None:
    decision = decide(engine(), forecast())
    [intent] = decision.intents
    [record] = decision.records
    candidate = record.candidate
    # entry at the ask; stop 1 and target 2 horizon sigma-hats (0.4 %) away
    assert candidate.entry_ref == 2000.1
    assert candidate.stop == pytest.approx(2000.1 * (1 - 0.004))
    assert candidate.target == pytest.approx(2000.1 * (1 + 2 * 0.004))
    # costs: 1 bp of spread + 2 x 0.5 bp of slippage + 2 x 3.5 / (100 x 2000) = 2.35 bp,
    # 0.05875 of a 0.4 % sigma-hat; conservative p = 0.6 - 1.645 x 0.05 = 0.51775
    assert candidate.ev_costs == pytest.approx(0.05875)
    assert candidate.p_forecast == 0.6
    assert candidate.p_win == pytest.approx(0.51775)
    assert candidate.ev_gross == pytest.approx(0.55325)
    assert candidate.ev_net == pytest.approx(0.55325 - 0.05875)
    assert record.outcome == "intent"
    assert record.filters["regime"] == REGIME_PLACEHOLDER
    assert record.model_versions == {"synthetic_momentum": "1"}
    assert intent == record.intent
    assert (intent.direction, intent.exposure, intent.entry_type) == ("long", 1.0, "market")
    assert (intent.stop, intent.target) == (candidate.stop, candidate.target)
    assert intent.time_stop == pd.Timestamp("2024-03-12 18:00", tz="UTC")
    # the risk engine takes its own lower bound: the intent carries the calibrated p and its error
    assert (intent.p_win, intent.p_se, intent.calibrated) == (0.6, 0.05, True)
    assert intent.signal_id == record.record_id == "S-template_barrier-000001"


def test_uncalibrated_forecasts_are_rejected() -> None:
    decision = decide(engine(), forecast(calibrated=False, calibration_id=None))
    assert decision.intents == []
    [record] = decision.records
    assert record.outcome == "rejected"
    assert record.reasons[0] == UNCALIBRATED
    assert record.intent is None


def test_every_candidate_is_recorded_and_the_best_is_chosen() -> None:
    # the short (p 0.5, net EV 0.19) qualifies but the long's net EV (0.49) is higher
    decision = decide(engine(), forecast("long", 0.6), forecast("short", 0.5))
    assert [r.outcome for r in decision.records] == ["intent", "not selected"]
    assert decision.records[1].reasons[0].startswith("S-template_barrier-000001 has a higher")
    assert decision.intents[0].direction == "long"
    # holding the long, the long is not selected and the short becomes the intent (a flip)
    held = decide(engine(), forecast("long", 0.6), forecast("short", 0.5), position_lots=0.4)
    assert [r.outcome for r in held.records] == ["not selected", "intent"]
    assert held.records[0].reasons == ("a position on this side is already held",)


def test_ev_filters_and_timing_reject_with_reasons() -> None:
    weak = decide(engine(), forecast("short", 0.45))  # conservative p 0.36775: net EV 0.0445
    [record] = weak.records
    assert record.outcome == "rejected"
    assert record.ev_reasons[0].startswith("EV_net 0.044")
    in_rollover = decide(
        engine(), forecast(ts=pd.Timestamp("2024-03-12 20:50", tz="UTC")), at="2024-03-12 20:50"
    )
    assert in_rollover.records[0].reasons == ("filter blackout: inside the rollover window",)
    assert in_rollover.records[0].filters["blackout"] == "inside the rollover window"
    stale = decide(engine(), forecast(ts=pd.Timestamp("2024-03-12 13:45", tz="UTC")))
    assert stale.records[0].reasons[0].startswith("forecast made at 2024-03-12 13:45")
    no_history = SignalEngine(SPEC, exact_costs(), SESSIONS)  # no spread reference yet
    blocked = decide(no_history, forecast())
    assert blocked.records[0].reasons == ("filter spread: no hour-of-week spread reference yet",)


def test_other_models_are_ignored_and_forecasts_need_a_barrier_probability() -> None:
    assert decide(engine(), forecast(model_id="other")).records == []
    assert decide(engine(), forecast(target_id="barrier.1h.tp1.sl1")).records == []
    with pytest.raises(ValueError, match="p_tp_first"):
        decide(engine(), forecast(p_tp_first=None, p_up=0.6, p_se=None))


def test_decisions_are_deterministic(tmp_path: Path) -> None:
    first = decide(engine(), forecast("long", 0.6), forecast("short", 0.5))
    second = decide(engine(), forecast("long", 0.6), forecast("short", 0.5))
    assert first == second
    copy = tmp_path / "copy.yaml"
    copy.write_text(TEMPLATE.read_text())
    assert load_strategy_spec(copy) == SPEC
