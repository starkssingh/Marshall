"""ROB-002: cost and latency stress — the R2 stressed-cost scenario passes exactly when the gross
edge covers the stressed costs, the break-even multiplier leaves no net P&L, every stress costs
more (spreads exactly in proportion), and latency eats a signal that is priced in over seconds in
proportion to the delay.

The markets are synthetic with a planted drift or jump: the edges are known by construction, so
their large Sharpe ratios test the mechanics, not a strategy."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from helpers.event_backtest import (
    CAPITAL,
    CFG,
    CLOCK,
    INSTRUMENT,
    SESSIONS,
    exact_costs,
    random_quotes,
)
from xq.backtest.costs import CostModel
from xq.backtest.vectorized import run_vectorized
from xq.robustness.costs_stress import (
    GATE_SCENARIO,
    CostScenario,
    cost_stress,
    plan_scenarios,
    stressed_costs,
    stressed_quotes,
)

GATES = CFG.gates_config()
COSTS = exact_costs()
S = pd.Timedelta(seconds=1)


def drifting_quotes(drift_per_day: float, seed: int) -> pd.DataFrame:
    """Random-walk quotes with a planted drift (price units per day)."""
    q = random_quotes("2024-03-04", "2024-04-06", seed=seed, every_s=60, step=0.05)
    days = (q["ts_utc"] - q["ts_utc"].iloc[0]).dt.total_seconds() / 86400
    return q.assign(bid=q["bid"] + drift_per_day * days, ask=q["ask"] + drift_per_day * days)


def intraday_longs() -> pd.Series:
    """Long from 13:00 to 19:00 UTC every weekday: 23 round trips, never over a rollover."""
    days = pd.bdate_range("2024-03-04", "2024-04-04", tz="UTC")
    times = [d + pd.Timedelta(hours=h) for d in days for h in (13, 19)]
    return pd.Series([1.0, 0.0] * len(days), index=pd.DatetimeIndex(times))


def stress(positions: pd.Series, quotes: pd.DataFrame, **kwargs: object):  # type: ignore[no-untyped-def]
    return cost_stress(
        positions,
        quotes,
        COSTS,
        CLOCK,
        capital=CAPITAL,
        periods_per_year=252,
        gates=GATES,
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_r2_scenario_passes_exactly_when_the_gross_edge_covers_the_stressed_costs() -> None:
    outcomes = []
    for drift in (2.5, 3.0, 4.0, 12.0):
        for seed in (1, 2):
            result = stress(intraday_longs(), drifting_quotes(drift, seed))
            table = result.table
            gross = table.loc["baseline", "gross_pnl"]
            stressed = table.loc[GATE_SCENARIO, "costs"]
            check = result.gate_check(GATES)
            assert check.passed == (gross > stressed)
            outcomes.append(check.passed)
    assert any(outcomes)  # both sides of the line are exercised
    assert not all(outcomes)


def test_a_thin_edge_profitable_at_baseline_fails_and_a_thick_one_passes() -> None:
    thin = stress(intraday_longs(), drifting_quotes(3.0, 1))
    ratio = thin.table.loc["baseline", "gross_pnl"] / thin.table.loc["baseline", "costs"]
    assert 1.05 < ratio < 1.5  # covers today's costs, not 1.5x spreads and 2x slippage
    assert thin.table.loc["baseline", "net_sharpe"] > 0
    assert not thin.gate_check(GATES).passed
    thick = stress(intraday_longs(), drifting_quotes(12.0, 1))
    assert thick.table.loc["baseline", "gross_pnl"] > 3 * thick.table.loc["baseline", "costs"]
    assert thick.gate_check(GATES).passed
    assert thick.break_even_multiplier > thin.break_even_multiplier > 1


def test_the_break_even_multiplier_leaves_no_net_pnl() -> None:
    positions, quotes = intraday_longs(), drifting_quotes(4.0, 2)
    result = stress(positions, quotes)
    k = result.break_even_multiplier
    baseline = result.table.loc["baseline"]
    assert k == pytest.approx(baseline["gross_pnl"] / baseline["costs"], rel=1e-3)  # near linear
    scenario = CostScenario.all_costs("check", k)
    at_k = run_vectorized(
        positions,
        stressed_quotes(quotes, k),
        stressed_costs(COSTS, scenario),
        CLOCK,
        capital=CAPITAL,
    )
    assert abs(at_k.daily["net_pnl"].sum()) < 1e-6 * baseline["costs"]
    losing = stress(intraday_longs(), drifting_quotes(-3.0, 1))
    assert losing.break_even_multiplier == 0.0  # no multiplier helps a strategy that loses gross


def test_every_stress_costs_more_and_spreads_scale_exactly() -> None:
    rng = np.random.default_rng(3)
    times = pd.date_range("2024-03-04 13:00", "2024-04-04 13:00", freq="7h", tz="UTC")
    positions = pd.Series(rng.choice([-1.0, 0.0, 0.5, 1.0], len(times)), index=times)
    quotes = random_quotes("2024-03-04", "2024-04-06", seed=3, every_s=60)
    table = stress(positions, quotes).table
    net = table["net_pnl"]
    assert list(table.index) == [s.name for s in plan_scenarios(GATES)]
    assert net["baseline"] > net["spread_x1.25"] > net["spread_x1.5"] > net["spread_x2"]
    assert net["baseline"] > net["slippage_x2"] > net["slippage_x3"]
    assert net["financing_x1.5"] < net["baseline"]  # positions are held over rollovers
    assert (
        table["gross_pnl"].drop(latency := [n for n in net.index if "latency" in n])
        == table.loc["baseline", "gross_pnl"]
    ).all()  # cost stress never touches the gross
    assert len(latency) == 3
    base = run_vectorized(positions, quotes, COSTS, CLOCK, capital=CAPITAL)
    wide = run_vectorized(positions, stressed_quotes(quotes, 2.0), COSTS, CLOCK, capital=CAPITAL)
    assert wide.fills["spread_cost"].sum() == pytest.approx(2 * base.fills["spread_cost"].sum())
    assert (wide.fills["fill_time"] == base.fills["fill_time"]).all()
    assert np.allclose(wide.fills["mid"], base.fills["mid"])


def test_latency_eats_a_signal_priced_in_over_seconds_in_proportion_to_the_delay() -> None:
    # at each decision the mid climbs 2.0 linearly over 10 s; the strategy is long for an hour
    days = pd.bdate_range("2024-03-04", "2024-03-28", tz="UTC")  # before Good Friday
    decisions = [d + pd.Timedelta(hours=14) for d in days]
    grid = []
    for t in decisions:
        grid += list(pd.date_range(t - 2 * S, t + 20 * S, freq="250ms"))
        grid += list(pd.date_range(t + 25 * S, t + pd.Timedelta(hours=1) + 120 * S, freq="30s"))
    ts = pd.DatetimeIndex(sorted(grid))
    since = (
        ts.to_numpy()[:, None] - pd.DatetimeIndex(decisions).to_numpy()[None, :]
    ) / np.timedelta64(1, "s")
    mid = 2000.0 + 2.0 * np.clip(since / 10.0, 0.0, 1.0).sum(axis=1)
    quotes = pd.DataFrame({"ts_utc": ts, "bid": mid - 0.1, "ask": mid + 0.1})
    exits = [t + pd.Timedelta(hours=1) for t in decisions]
    positions = pd.Series(
        [1.0] * len(decisions) + [0.0] * len(exits), index=pd.DatetimeIndex(decisions + exits)
    ).sort_index()
    table = stress(positions, quotes).table
    assert (table["fills"] == 2 * len(decisions)).all()  # every entry and exit filled
    assert (table["missed"] == 0).all()
    gross = table["gross_pnl"]
    # the model's own latency is 1 s: the fill captures (10 - latency) / 10 of each climb
    captured = {
        "baseline": 0.9,
        "latency_+250ms": 0.875,
        "latency_+1000ms": 0.8,
        "latency_+5000ms": 0.4,
    }
    for name, share in captured.items():
        assert gross[name] / gross["baseline"] == pytest.approx(share / 0.9, rel=2e-3)


def test_credited_financing_is_reduced_and_scenarios_are_checked() -> None:
    credit = COSTS.config.model_copy(
        update={
            "provisional": False,
            "financing": COSTS.config.financing.model_copy(update={"short_rate_annual_pct": -2.0}),
        }
    )
    model = CostModel(credit, INSTRUMENT, SESSIONS)
    stressed = stressed_costs(model, CostScenario("f", financing=1.5, extra_latency_ms=250))
    assert stressed.config.financing.short_rate_annual_pct == pytest.approx(-2.0 / 1.5)
    assert stressed.config.financing.long_rate_annual_pct == pytest.approx(3.6 * 1.5)
    assert stressed.config.latency_ms == COSTS.config.latency_ms + 250
    assert stressed_quotes(pd.DataFrame({"bid": [9.0], "ask": [11.0]}), 3.0).to_dict("list") == {
        "bid": [7.0],
        "ask": [13.0],
    }
    with pytest.raises(ValueError, match="negative"):
        CostScenario("bad", spread=-1.0)
    base = CostScenario("baseline")
    with pytest.raises(ValueError, match="distinct"):
        stress(intraday_longs(), drifting_quotes(1.0, 1), scenarios=[base, replace(base)])
    only = stress(intraday_longs(), drifting_quotes(1.0, 1), scenarios=[base])
    assert list(only.table.index) == ["baseline", GATE_SCENARIO]  # the gate's is always run
