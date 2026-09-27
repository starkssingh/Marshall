"""RISK-006: the kill switch (file, environment, manual) and the data-health breakers block new
exposure — exactly past their thresholds — never an exit; with the flatten policy the event
engine closes the position; the breakers block new orders in an event backtest."""

from pathlib import Path

import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CFG,
    CLOCK,
    RISK,
    SIGMA,
    ScriptedStrategy,
    exact_costs,
    market_state,
    ns,
    quotes,
    risk_state,
)
from xq.backtest.engine import MarketData, run_event_backtest
from xq.backtest.events import Event
from xq.core.config import KillSwitchConfig
from xq.core.types import Side, Timeframe
from xq.risk.engine import RISK_RULE
from xq.risk.kill_switch import KillSwitch, breaker_reasons
from xq.risk.state import MarketState
from xq.signals.schema import TradeIntent

BREAKERS = CFG.risk_config().breakers
AT = pd.Timestamp("2024-03-12 14:00", tz="UTC")


def stamped(intent: TradeIntent) -> TradeIntent:
    return intent.model_copy(update={"intent_id": "I000001", "created_at": AT})


LONG = stamped(TradeIntent(direction="long", exposure=1.0, stop=1990.1))
SHORT = stamped(TradeIntent(direction="short", exposure=1.0, stop=2009.9))
FLAT = stamped(TradeIntent(direction="flat"))


def test_the_breakers_are_the_provisional_profile() -> None:
    assert (BREAKERS.stale_quote_s, BREAKERS.spread_multiple, BREAKERS.spread_window) == (
        120.0,
        5.0,
        500,
    )
    kill = CFG.risk_config().kill_switch
    assert (kill.file, kill.env_var, kill.flatten) == (None, "XQ_KILL_SWITCH", False)


def test_the_kill_switch_reads_its_file_its_variable_and_the_manual_flag(tmp_path: Path) -> None:
    flag = tmp_path / "STOP"
    config = KillSwitchConfig(file=flag, env_var="XQ_KILL_SWITCH")
    environ: dict[str, str] = {}
    switch = KillSwitch(config, environ=environ)
    assert switch.reason() is None
    flag.touch()
    assert switch.reason() == f"kill switch: {flag} exists"
    flag.unlink()
    for value, on in (("1", True), ("true", True), (" ON ", True), ("0", False), ("", False)):
        environ["XQ_KILL_SWITCH"] = value
        assert (switch.reason() is not None) is on
    environ.clear()
    switch.engage("operator")
    assert switch.reason() == "kill switch: operator"
    switch.release()
    assert switch.reason() is None
    assert not switch.flatten


def test_a_stale_quote_trips_its_breaker_only_past_the_limit() -> None:
    fresh = market_state()
    assert breaker_reasons(fresh, BREAKERS) == []
    quoted = ns("2024-03-12 14:00")
    at_limit = MarketState(ns("2024-03-12 14:02"), 1999.9, 2000.1, quoted)
    assert at_limit.quote_age_s == 120.0
    assert breaker_reasons(at_limit, BREAKERS) == []
    stale = MarketState(ns("2024-03-12 14:02") + 1, 1999.9, 2000.1, quoted)
    (reason,) = breaker_reasons(stale, BREAKERS)
    assert reason.startswith("stale quote")


def test_an_abnormal_spread_trips_its_breaker_only_past_the_multiple() -> None:
    # spread 0.2: at most 5 x the median 0.04 = 0.2
    assert breaker_reasons(market_state(spread_reference=0.04), BREAKERS) == []
    (reason,) = breaker_reasons(market_state(spread_reference=0.0399), BREAKERS)
    assert reason.startswith("abnormal spread")
    assert breaker_reasons(market_state(spread_reference=None), BREAKERS) == []


def test_the_risk_engine_blocks_new_exposure_but_never_an_exit() -> None:
    killed = market_state(kill_reason="kill switch: operator")
    entry = RISK.evaluate(LONG, risk_state(), killed)
    assert not entry.approved
    assert entry.reasons == ("kill switch: operator",)
    assert RISK.evaluate(FLAT, risk_state(0.5), killed).approved
    flip = RISK.evaluate(SHORT, risk_state(0.5), killed)
    assert (flip.approved, flip.side, flip.target_lots) == (True, Side.SELL, 0.0)
    assert flip.reasons[0] == f"{RISK_RULE}kill switch: operator"
    wide = market_state(spread_reference=0.01)
    (reason,) = RISK.evaluate(LONG, risk_state(), wide).reasons
    assert reason.startswith("abnormal spread")
    assert RISK.evaluate(FLAT, risk_state(-0.5), wide).approved


def run(script: dict[str, list[TradeIntent]], q: pd.DataFrame, **kwargs: object) -> object:
    return run_event_backtest(
        ScriptedStrategy(script),
        MarketData.from_ticks(q, Timeframe.M15),
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
        risk=RISK,
        sigma_daily=SIGMA,
        **kwargs,  # type: ignore[arg-type]
    )


def test_breakers_block_new_orders_in_an_event_backtest() -> None:
    rows = [(f"2024-03-12 13:{m:02d}:00", 1999.9, 2000.1) for m in range(0, 60, 2)]
    q = quotes(
        *rows,
        ("2024-03-12 14:05:00", 1999.9, 2000.1),  # the last quote before 14:15: 10 min old
        ("2024-03-12 14:20:00", 1999.9, 2000.1),
        ("2024-03-12 14:30:00", 1998.0, 2002.0),  # a spread of 4: 20 x the median of 0.2
        ("2024-03-12 14:40:00", 1999.9, 2000.1),
        ("2024-03-12 14:45:00", 1999.9, 2000.1),  # healthy again: the entry goes through
        ("2024-03-12 14:45:02", 1999.9, 2000.1),
    )
    long = TradeIntent(direction="long", exposure=1.0, stop=1990.1)
    script = {t: [long] for t in ("2024-03-12 14:15", "2024-03-12 14:30", "2024-03-12 14:45")}
    result = run(script, q)
    decisions = result.ledger.loc[result.ledger["kind"] == "decision"]  # type: ignore[attr-defined]
    assert decisions["approved"].tolist() == [False, False, True]
    assert decisions["reason"].iloc[0].startswith("stale quote: the latest is 600 s old")
    assert decisions["reason"].iloc[1].startswith("abnormal spread: 4 is more than 5 x")
    assert result.fills["role"].tolist() == ["entry"]  # type: ignore[attr-defined]


def test_the_kill_switch_stops_entries_and_its_flatten_policy_closes(tmp_path: Path) -> None:
    flag = tmp_path / "KILL"
    q = quotes(
        *[(f"2024-03-12 14:{m:02d}:00", 1999.9, 2000.1) for m in range(0, 60, 1)],
        *[(f"2024-03-12 15:{m:02d}:00", 1999.9, 2000.1) for m in range(0, 60, 1)],
    )
    long = TradeIntent(direction="long", exposure=1.0, stop=1990.1)
    script = {"2024-03-12 14:15": [long], "2024-03-12 15:00": [long.model_copy()]}

    def kill_at(instant: str) -> object:
        def observer(event: Event, now: int) -> None:
            if now >= ns(instant):
                flag.touch()

        return observer

    for flatten in (False, True):
        flag.unlink(missing_ok=True)
        switch = KillSwitch(KillSwitchConfig(file=flag, flatten=flatten), environ={})
        result = run(script, q, kill_switch=switch, observer=kill_at("2024-03-12 14:31"))
        ledger = result.ledger  # type: ignore[attr-defined]
        decisions = ledger.loc[ledger["kind"] == "decision"]
        intents = ledger.loc[ledger["kind"] == "intent", "reason"].tolist()
        fills = result.fills  # type: ignore[attr-defined]
        # the 15:00 long is refused while the switch is on
        assert not bool(decisions["approved"].iloc[-1])
        assert decisions["reason"].iloc[-1] == f"kill switch: {flag} exists"
        if not flatten:  # the long is held
            assert intents == ["", ""]
            assert fills["role"].tolist() == ["entry"]
        else:
            # at the first bar after 14:31 (14:45) the engine sends a flat through the risk engine
            assert intents == ["", "kill switch: flatten", ""]
            assert fills["role"].tolist() == ["entry", "exit"]
            assert fills["fill_time"].iloc[1] > pd.Timestamp("2024-03-12 14:45", tz="UTC")
        assert result.link_problems == ()  # type: ignore[attr-defined]


def test_backtests_ignore_the_machines_kill_switch_unless_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XQ_KILL_SWITCH", "1")
    q = quotes(
        ("2024-03-12 14:00:00", 1999.9, 2000.1),
        ("2024-03-12 14:15:00", 1999.9, 2000.1),
        ("2024-03-12 14:15:02", 1999.9, 2000.1),
    )
    script = {"2024-03-12 14:15": [TradeIntent(direction="long", exposure=1.0, stop=1990.1)]}
    assert run(script, q).fills["role"].tolist() == ["entry"]  # type: ignore[attr-defined]
    live = KillSwitch(CFG.risk_config().kill_switch)  # reads the environment
    assert run(script, q, kill_switch=live).fills.empty  # type: ignore[attr-defined]
