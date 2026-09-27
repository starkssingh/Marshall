"""RISK-005: `RiskEngine.evaluate` on hand-computed cases — exits always approved, the entry
pipeline (data, stop policy, sizing, halts, caps), refusals that still close an opposite
position, purity and the profile's version — and the halts end to end in the event backtester."""

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
    quotes,
    risk_engine,
    risk_state,
)
from xq.backtest.engine import MarketData, run_event_backtest
from xq.core.types import Side, Timeframe
from xq.risk.engine import RISK_RULE, RiskEngine, profile_hash
from xq.signals.schema import TradeIntent

AT = pd.Timestamp("2024-03-12 14:00", tz="UTC")


def stamped(intent: TradeIntent, n: int = 1) -> TradeIntent:
    return intent.model_copy(update={"intent_id": f"I{n:06d}", "created_at": AT})


def long(exposure: float = 1.0, stop: float | None = 1990.1, **fields: object) -> TradeIntent:
    return stamped(TradeIntent(direction="long", exposure=exposure, stop=stop, **fields))  # type: ignore[arg-type]


def short(exposure: float = 1.0, stop: float | None = 2009.9, **fields: object) -> TradeIntent:
    return stamped(TradeIntent(direction="short", exposure=exposure, stop=stop, **fields))  # type: ignore[arg-type]


FLAT = stamped(TradeIntent(direction="flat"))


def test_an_entry_risks_half_a_percent_to_its_stop() -> None:
    # 0.5 % of 100,000 = 500 USD over (2000.1 - 1990.1) x 100 oz = 0.5 lots; exposure 1.0 at the
    # mid of 2000 is 0.5 lots too
    decision = RISK.evaluate(long(), risk_state(), market_state())
    assert decision.approved
    assert decision.issued
    assert (decision.side, decision.size_lots, decision.target_lots) == (Side.BUY, 0.5, 0.5)
    assert (decision.order_type, decision.price) == ("market", None)
    assert decision.adjusted_stop == 1990.1
    assert decision.decision_id == "D-I000001"
    assert decision.reasons == ("entry: sized by the risk engine",)
    assert decision.config_version == RISK.config_version == f"risk-1@{profile_hash(RISK.config)}"
    snapshot = decision.limits_snapshot
    assert snapshot["method_lots"] == pytest.approx(0.5)
    assert snapshot["requested_lots"] == pytest.approx(0.5)
    assert snapshot["equity"] == CAPITAL
    assert snapshot["sigma_daily"] == 0.01
    assert snapshot["max_drawdown"] == 0.15
    # a stop twice as far halves the size; a smaller exposure caps it (0.3 x 100,000 / 200,000)
    assert RISK.evaluate(long(stop=1980.1), risk_state(), market_state()).size_lots == 0.25
    assert RISK.evaluate(long(exposure=0.3), risk_state(), market_state()).size_lots == 0.15


def test_sizing_uses_equity_and_the_drawdown_throttle() -> None:
    richer = risk_state(equity=120_000.0)
    assert RISK.evaluate(long(exposure=10.0), richer, market_state()).size_lots == 0.6
    # 10 % drawdown: halfway between 5 % and 15 %, half the size
    down = risk_state(drawdown=0.10, worst_drawdown=0.10)
    assert RISK.evaluate(long(), down, market_state()).size_lots == 0.25


def test_only_a_calibrated_probability_scales_the_size() -> None:
    uncalibrated = RISK.evaluate(long(p_win=0.58), risk_state(), market_state())
    assert not uncalibrated.approved
    assert uncalibrated.reasons == ("an uncalibrated win probability cannot size a position",)
    # 0.55 is halfway from 0.5 (no size) to 0.6 (full size)
    calibrated = RISK.evaluate(long(p_win=0.55, calibrated=True), risk_state(), market_state())
    assert calibrated.size_lots == 0.25
    zero = RISK.evaluate(long(p_win=0.5, calibrated=True), risk_state(), market_state())
    assert zero.approved
    assert zero.side is None
    assert zero.reasons[-2:] == ("sized to zero lots", "target unchanged: no order")


def test_flips_changes_and_unchanged_targets() -> None:
    flip = RISK.evaluate(short(), risk_state(0.34), market_state())
    assert (flip.side, flip.size_lots, flip.target_lots) == (Side.SELL, 0.84, -0.5)
    increase = RISK.evaluate(long(), risk_state(0.2), market_state())
    assert (increase.side, increase.size_lots) == (Side.BUY, 0.3)
    reduce = RISK.evaluate(long(exposure=0.2), risk_state(0.5), market_state())
    assert (reduce.side, reduce.size_lots, reduce.target_lots) == (Side.SELL, 0.4, 0.1)
    same = RISK.evaluate(long(), risk_state(0.5), market_state())
    assert same.approved
    assert same.side is None
    assert same.reasons[-1] == "target unchanged: no order"


def test_exits_are_always_approved() -> None:
    halted = risk_state(-0.5, worst_drawdown=0.4, day_pnl=-9_000.0, trades_today=99)
    exit_ = RISK.evaluate(FLAT, halted, None)  # no quote, every halt in force
    assert exit_.approved
    assert (exit_.side, exit_.size_lots, exit_.target_lots) == (Side.BUY, 0.5, 0.0)
    assert (exit_.order_type, exit_.adjusted_stop, exit_.target) == ("market", None, None)
    assert exit_.reasons == ("exit: always allowed",)
    nothing = RISK.evaluate(FLAT, risk_state(), None)
    assert nothing.approved
    assert nothing.side is None


def test_refused_entries_are_rejected_but_an_opposite_position_is_closed() -> None:
    blind = RISK.evaluate(long(), risk_state(), None)
    assert not blind.approved
    assert blind.reasons == ("no quote known at the decision time",)
    assert blind.issued
    stopless = RISK.evaluate(long(stop=None), risk_state(), market_state())
    assert stopless.reasons == ("an entry needs a stop (RISK-004)",)
    unbounded = RISK.evaluate(long(), risk_state(), market_state(sigma=None))
    assert unbounded.reasons[0].startswith("no sigma-hat")
    # the same refusals while short: the strategy wants to be long, so the short is closed
    closing = RISK.evaluate(long(stop=None), risk_state(-0.3), market_state())
    assert closing.approved
    assert (closing.side, closing.size_lots, closing.target_lots) == (Side.BUY, 0.3, 0.0)
    assert closing.reasons[0] == f"{RISK_RULE}an entry needs a stop (RISK-004)"
    assert closing.adjusted_stop is None
    # while long, the position is held (and keeps its bracket): nothing is sent
    held = RISK.evaluate(long(stop=None), risk_state(0.3), market_state())
    assert not held.approved


def test_halts_refuse_new_exposure_but_not_a_reduction() -> None:
    halted = {"worst_drawdown": 0.15, "drawdown": 0.02}
    entry = RISK.evaluate(long(), risk_state(**halted), market_state())
    assert not entry.approved
    assert entry.reasons[0].startswith("drawdown halt")
    increase = RISK.evaluate(long(), risk_state(0.2, **halted), market_state())
    assert not increase.approved
    reduce = RISK.evaluate(long(exposure=0.2), risk_state(0.5, **halted), market_state())
    assert (reduce.approved, reduce.side, reduce.target_lots) == (True, Side.SELL, 0.1)
    flip = RISK.evaluate(short(), risk_state(0.5, **halted), market_state())
    assert (flip.approved, flip.side, flip.target_lots) == (True, Side.SELL, 0.0)
    assert flip.reasons[0].startswith(f"{RISK_RULE}drawdown halt")
    below = {"worst_drawdown": 0.1499999, "drawdown": 0.02}
    assert RISK.evaluate(long(), risk_state(**below), market_state()).approved


def test_caps_bound_the_approved_target() -> None:
    engine = risk_engine(risk_per_trade=0.5)  # the budget no longer binds
    capped = engine.evaluate(long(exposure=5.0), risk_state(), market_state())
    # 3 x 100,000 USD of notional at the mid of 2000 = 1.5 lots
    assert capped.target_lots == 1.5
    assert capped.reasons[-1] == "capped by max_notional"
    tight = risk_engine(risk_per_trade=0.5, max_lots=0.8)
    assert tight.evaluate(long(exposure=5.0), risk_state(), market_state()).target_lots == 0.8


def test_a_close_stop_is_widened_and_a_limit_entry_keeps_its_price() -> None:
    widened = RISK.evaluate(long(stop=1999.9), risk_state(), market_state())
    assert widened.adjusted_stop == 1999.5  # three spreads below the ask
    assert widened.reasons[1].startswith("stop widened from 1999.9 to 1999.5")
    limit = RISK.evaluate(
        long(entry_type="limit", limit_price=1995.0, stop=1985.0), risk_state(), market_state()
    )
    assert (limit.order_type, limit.price, limit.adjusted_stop) == ("limit", 1995.0, 1985.0)
    assert limit.limits_snapshot["entry_reference"] == 1995.0
    assert limit.limits_snapshot["stop_distance"] == pytest.approx(10.0)


def test_the_engine_is_pure_and_its_version_names_the_profile() -> None:
    state, market = risk_state(0.1), market_state()
    first, second = RISK.evaluate(long(), state, market), RISK.evaluate(long(), state, market)
    assert first == second
    assert state == risk_state(0.1)  # inputs are not changed
    looser = risk_engine(max_lots=21)
    assert looser.config_version != RISK.config_version
    assert looser.config_version.startswith("risk-1@")
    assert "PROVISIONAL" in RISK.label
    with pytest.raises(ValueError, match="stamps"):
        RISK.evaluate(TradeIntent(direction="flat"), state, market)
    with pytest.raises(ValueError, match="margin_rate"):
        RiskEngine(CFG.risk_config(), RISK.instrument, margin_rate=0.0)


def test_a_daily_loss_halts_entries_until_the_next_trading_day_but_not_exits() -> None:
    q = quotes(
        ("2024-03-12 14:00:00", 1999.9, 2000.1),
        ("2024-03-12 14:15:00", 1999.9, 2000.1),  # long 0.49 lots, stop 1990 (10.1 below)
        ("2024-03-12 14:15:02", 1999.9, 2000.1),
        ("2024-03-12 14:20:00", 1929.9, 1930.1),  # gaps through the stop: -3,430 USD, -3.4 %
        ("2024-03-12 14:30:00", 1929.9, 1930.1),  # a new long: halted
        ("2024-03-12 14:45:00", 1929.9, 1930.1),  # a flat: approved (nothing to close)
        ("2024-03-12 22:20:00", 1929.9, 1930.1),
        ("2024-03-12 22:30:00", 1929.9, 1930.1),  # Wednesday's trading day: allowed again
        ("2024-03-12 22:30:02", 1929.9, 1930.1),
    )
    script = {
        "2024-03-12 14:15": [TradeIntent(direction="long", exposure=1.0, stop=1990.0)],
        "2024-03-12 14:30": [TradeIntent(direction="long", exposure=1.0, stop=1920.0)],
        "2024-03-12 14:45": [TradeIntent(direction="flat")],
        "2024-03-12 22:30": [TradeIntent(direction="long", exposure=1.0, stop=1920.0)],
    }
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
    decisions = result.ledger.loc[result.ledger["kind"] == "decision"]
    assert decisions["approved"].tolist() == [True, False, True, True]
    assert decisions["reason"].iloc[1].startswith("daily loss halt: 3.4")
    assert result.fills["role"].tolist() == ["entry", "stop_loss", "entry"]
    assert result.link_problems == ()


def test_a_session_cap_applies_while_its_session_is_in_force() -> None:
    capped = risk_engine(session_max_exposure={"tokyo": 0.2})
    q = quotes(
        ("2024-03-12 01:50:00", 1999.9, 2000.1),
        ("2024-03-12 02:00:00", 1999.9, 2000.1),  # 11:00 in Tokyo: at most 0.2 x equity
        ("2024-03-12 02:00:02", 1999.9, 2000.1),
        ("2024-03-12 13:50:00", 1999.9, 2000.1),
        ("2024-03-12 14:00:00", 1999.9, 2000.1),  # 23:00 in Tokyo: no session cap
        ("2024-03-12 14:00:02", 1999.9, 2000.1),
    )
    script = {
        "2024-03-12 02:00": [TradeIntent(direction="long", exposure=1.0, stop=1990.1)],
        "2024-03-12 14:00": [TradeIntent(direction="long", exposure=1.0, stop=1990.1)],
    }
    result = run_event_backtest(
        ScriptedStrategy(script),
        MarketData.from_ticks(q, Timeframe.M15),
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
        risk=capped,
        sigma_daily=SIGMA,
    )
    # 0.2 x 100,000 / (2,000 x 100 oz) = 0.1 lots in Tokyo; outside it 0.5 % of the equity left
    # after the first fill's costs (just under 100,000) over 10 x 100 oz, 0.49 lots
    assert result.fills["position_lots"].tolist() == [0.1, 0.49]
    decisions = result.ledger.loc[result.ledger["kind"] == "decision", "reason"]
    assert decisions.iloc[0].endswith("capped by session tokyo")
    unknown = risk_engine(session_max_exposure={"lunch": 0.2})
    with pytest.raises(ValueError, match="unknown sessions"):
        run_event_backtest(
            ScriptedStrategy(script),
            MarketData.from_ticks(q, Timeframe.M15),
            exact_costs(),
            CLOCK,
            capital=CAPITAL,
            margin_rate=0.05,
            risk=unknown,
        )
