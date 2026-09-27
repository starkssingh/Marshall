"""BT-008: session constraints — entry blackouts 16:45-18:15 New York, around US data releases and
before the weekly close; no entry ever fills in a blackout while exits do; the optional flat
before the weekend."""

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CFG,
    CLOCK,
    SESSIONS,
    RandomStrategy,
    ScriptedStrategy,
    exact_costs,
    ns,
    quotes,
    random_quotes,
)
from xq.backtest.constraints import SessionConstraints
from xq.backtest.engine import EventBacktestResult, MarketData, run_event_backtest
from xq.core.config import EventBacktestConfig
from xq.core.types import Timeframe
from xq.datasets.calendar_columns import calendar_columns
from xq.signals.schema import TradeIntent

DEFAULT = CFG.backtest_config().event_config()


def constraints(**changes: object) -> SessionConstraints:
    config = EventBacktestConfig.model_validate({**DEFAULT.model_dump(), **changes})
    return SessionConstraints(SESSIONS, CLOCK, config)


def run(
    strategy: object, q: pd.DataFrame, rules: SessionConstraints, tf: Timeframe = Timeframe.M15
) -> EventBacktestResult:
    return run_event_backtest(
        strategy,  # type: ignore[arg-type]
        MarketData.from_ticks(q, tf),
        exact_costs(),
        CLOCK,
        capital=CAPITAL,
        margin_rate=0.05,
        constraints=rules,
    )


def test_the_configured_blackouts_are_the_rollover_and_us_release_windows() -> None:
    assert DEFAULT.blackouts.event_windows == ["rollover", "us_data_release"]
    assert DEFAULT.blackouts.before_weekly_close_min == 60
    rollover = SESSIONS.event_windows["rollover"]
    assert (str(rollover.start), str(rollover.end)) == ("16:45:00", "18:15:00")  # type: ignore[union-attr]


def test_blackout_intervals_agree_with_the_dataset_calendar_columns() -> None:
    # March 2024: US DST starts on the 10th, UK DST on the 31st
    rng = np.random.default_rng(0)
    start, end = ns("2024-03-01"), ns("2024-04-05")
    t = np.sort(rng.integers(start, end, 20_000))
    t = np.concatenate([t, np.arange(start, end, 60_000_000_000)])  # every minute boundary too
    columns = calendar_columns(pd.DatetimeIndex(pd.to_datetime(t, unit="ns", utc=True)), SESSIONS)
    rules = constraints(blackouts={"event_windows": ["rollover", "us_data_release"]})
    reasons = [rules.entry_blackout(int(x)) for x in t]
    rollover = np.array([r == "rollover window" for r in reasons])
    np.testing.assert_array_equal(rollover, columns["in_rollover_window"].to_numpy(bool))
    release = columns["in_us_data_release_window"].to_numpy(bool) & ~rollover
    np.testing.assert_array_equal(
        np.array([r == "us_data_release window" for r in reasons]), release
    )


def test_rollover_blackout_is_1645_to_1815_new_york_across_the_dst_change() -> None:
    rules = constraints(blackouts={"event_windows": ["rollover"]})
    # EST before 10 March (UTC-5), EDT after (UTC-4)
    assert rules.entry_blackout(ns("2024-03-07 21:44:59")) is None
    assert rules.entry_blackout(ns("2024-03-07 21:45:00")) == "rollover window"
    assert rules.entry_blackout(ns("2024-03-07 23:14:59")) == "rollover window"
    assert rules.entry_blackout(ns("2024-03-07 23:15:00")) is None
    assert rules.entry_blackout(ns("2024-03-12 20:45:00")) == "rollover window"
    assert rules.entry_blackout(ns("2024-03-12 22:15:00")) is None
    assert rules.entry_blackout(ns("2024-03-10 22:00:00")) == "rollover window"  # Sunday reopen


def test_the_weekly_close_blackout_covers_fridays_and_the_day_before_good_friday() -> None:
    rules = constraints(blackouts={"event_windows": [], "before_weekly_close_min": 60})
    friday_close = ns("2024-03-15 21:00")
    assert friday_close in set(rules.weekly_closes.tolist())
    assert rules.entry_blackout(friday_close - 3_600_000_000_000) is not None
    assert rules.entry_blackout(friday_close - 3_600_000_000_001) is None
    assert rules.entry_blackout(ns("2024-03-14 20:30")) is None  # an ordinary Thursday
    # Good Friday (29 March 2024) is closed: Thursday's close starts a long weekend
    assert "weekly close" in str(rules.entry_blackout(ns("2024-03-28 20:30")))


def test_no_entry_fills_inside_a_blackout_but_exits_do() -> None:
    rules = constraints()
    q = random_quotes("2024-03-11 00:00", "2024-03-16 00:00", seed=8, every_s=20)
    result = run(RandomStrategy(seed=8, trade_probability=0.5), q, rules, Timeframe.M5)
    fills = result.fills
    times = [int(pd.Timestamp(t).value) for t in fills["fill_time"]]
    blocked = np.array([rules.entry_blackout(t) is not None for t in times])
    entries = (fills["role"] == "entry").to_numpy()
    assert entries.sum() > 50
    assert not (blocked & entries).any()
    assert (blocked & ~entries).any()  # exits and stops inside blackouts are allowed
    refused = result.refusals["reason"]
    assert refused.str.startswith("entry blackout").sum() > 5
    assert result.link_problems == ()


def test_a_market_entry_meeting_the_rollover_window_is_cancelled() -> None:
    rules = constraints()
    q = quotes(
        ("2024-03-12 20:43:30", 1999.9, 2000.1),
        ("2024-03-12 20:44:00", 1999.9, 2000.1),  # decision at 16:44 New York, outside the window
        ("2024-03-12 20:45:00.5", 1999.9, 2000.1),  # the first quote after arrival: inside it
        ("2024-03-12 20:46:00", 1999.9, 2000.1),
    )
    script = {"2024-03-12 20:44": [TradeIntent(direction="long", exposure=1.0)]}
    result = run(ScriptedStrategy(script), q, rules, Timeframe.M1)
    assert result.fills.empty
    ledger = result.ledger
    [cancel] = ledger.loc[ledger["kind"] == "order_cancelled", "reason"]
    assert cancel == "entry blackout: rollover window"


def test_a_stop_fills_inside_the_blackout() -> None:
    q = quotes(
        ("2024-03-12 20:00:00", 1999.9, 2000.1),
        ("2024-03-12 20:15:00", 1999.9, 2000.1),  # decision (long, stop 1995)
        ("2024-03-12 20:15:02", 1999.9, 2000.1),  # the entry
        ("2024-03-12 20:50:00", 1989.9, 1990.1),  # 16:50 New York, in the window
    )
    script = {"2024-03-12 20:15": [TradeIntent(direction="long", exposure=0.5, stop=1995.0)]}
    result = run(ScriptedStrategy(script), q, constraints())
    assert result.fills["role"].tolist() == ["entry", "stop_loss"]
    assert result.fills["fill_time"].iloc[1] == pd.Timestamp("2024-03-12 20:50", tz="UTC")


def test_a_resting_entry_waits_out_the_blackout() -> None:
    q = quotes(
        ("2024-03-12 20:15:00", 1999.9, 2000.1),
        ("2024-03-12 20:30:00", 1999.9, 2000.1),  # decision: buy limit at 1985
        ("2024-03-12 20:50:00", 1983.9, 1984.1),  # at the price, in the window: waits
        ("2024-03-12 22:05:00", 1983.9, 1984.1),  # reopen, still in the window: waits
        ("2024-03-12 22:16:00", 1983.9, 1984.1),  # after 18:15 New York: fills at its price
    )
    limit = TradeIntent(direction="long", exposure=0.5, entry_type="limit", limit_price=1985.0)
    result = run(ScriptedStrategy({"2024-03-12 20:30": [limit]}), q, constraints())
    [fill] = result.fills.itertuples()
    assert fill.fill_time == pd.Timestamp("2024-03-12 22:16", tz="UTC")
    assert fill.price == 1985.0
    unconstrained = run(
        ScriptedStrategy({"2024-03-12 20:30": [limit]}),
        q,
        constraints(blackouts={"event_windows": [], "before_weekly_close_min": 0}),
    )
    assert unconstrained.fills["fill_time"].iloc[0] == pd.Timestamp("2024-03-12 20:50", tz="UTC")


def test_flat_before_the_weekend_closes_the_position_before_the_weekly_close() -> None:
    q = random_quotes("2024-03-14 00:00", "2024-03-19 00:00", seed=4, every_s=60)
    script = {"2024-03-15 14:00": [TradeIntent(direction="long", exposure=1.0)]}
    kept = run(ScriptedStrategy(script), q, constraints())
    assert kept.fills["role"].tolist() == ["entry"]
    flat = run(ScriptedStrategy(script), q, constraints(flat_before_weekend=True))
    assert flat.fills["role"].tolist() == ["entry", "exit"]
    exit_time = flat.fills["fill_time"].iloc[1]
    assert ns("2024-03-15 20:30:01") <= exit_time.value < ns("2024-03-15 20:35")  # 16:30 New York
    intents = flat.ledger.loc[flat.ledger["kind"] == "intent"]
    assert intents["reason"].tolist() == ["", "flat before the weekly close"]
    # the Friday rollover is not financed on the flattened account
    assert (flat.financing.index < pd.Timestamp("2024-03-15 20:30", tz="UTC")).all()
    assert kept.financing.index.max() >= pd.Timestamp("2024-03-15 21:00", tz="UTC")


def test_blackout_windows_must_be_configured_event_windows() -> None:
    with pytest.raises(Exception, match="not configured event windows"):
        constraints(blackouts={"event_windows": ["lunch"]})
