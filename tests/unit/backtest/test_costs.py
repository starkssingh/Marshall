"""BT-001: golden cost cases — commission, slippage multipliers, financing incl. triple rollover."""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.pipeline import REPO
from xq.backtest.costs import SCREENING_LABEL, CostModel
from xq.core.config import CostModelConfig, load_config
from xq.core.errors import ConfigError
from xq.data.spreads import NoSpreadDataError

CFG = load_config("research", config_dir=REPO / "config")
PRICE = 2000.0  # 1 lot = 100 oz = 200,000 USD notional


def model(**changes: object) -> CostModel:
    data = CFG.cost_model_config().model_dump()
    for dotted, value in changes.items():
        section, key = dotted.split("__")
        data[section] = {**data[section], key: value}
    return CostModel(
        CostModelConfig.model_validate(data), CFG.instrument("xauusd"), CFG.sessions_config()
    )


def utc(*times: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(list(times), utc=True))


def test_the_placeholder_model_is_marked_provisional() -> None:
    config = CFG.cost_model_config()
    assert config.provisional
    assert CFG.backtest_config().cost_model == "placeholder"
    assert model().latency == pd.Timedelta(seconds=1)
    assert model().max_fill_delay == pd.Timedelta(seconds=300)


def test_commission_per_lot_and_per_notional() -> None:
    np.testing.assert_allclose(model().commission_usd([2.0, -0.5], PRICE), [7.0, 1.75])
    by_notional = model(
        commission__per_lot_per_side_usd=0.0, commission__per_notional_per_side_bps=0.5
    )
    np.testing.assert_allclose(by_notional.commission_usd([1.0], PRICE), [10.0])  # 0.5 bp of 200k


def test_slippage_scales_with_sigma_and_the_largest_window_multiplier() -> None:
    times = utc(
        "2024-03-12 15:00",  # 11:00 New York: no window
        "2024-03-12 20:50",  # 16:50 New York: rollover window
        "2024-03-12 12:35",  # 08:35 New York: US data release window
    )
    np.testing.assert_allclose(model().slippage_bps(times, [2.0, 2.0, 2.0]), [0.7, 2.1, 1.4])
    both = model(slippage__multipliers={"rollover_window": 3.0, "us_data_release_window": 4.0})
    release_at_rollover = utc("2024-03-12 12:35")
    np.testing.assert_allclose(both.slippage_bps(release_at_rollover, [0.0]), [2.0])
    with pytest.raises(ValueError, match="negative"):
        model().slippage_bps(times[:1], [-1.0])


def test_rollovers_are_daily_with_the_triple_weekday() -> None:
    # Week of 11 March 2024 (EDT): rollovers at 21:00 UTC, Monday to Friday.
    rolls = model().rollovers(
        pd.Timestamp("2024-03-10 22:00", tz="UTC"), pd.Timestamp("2024-03-17 22:00", tz="UTC")
    )
    assert rolls.index.tolist() == [
        pd.Timestamp(f"2024-03-{d} 21:00", tz="UTC") for d in (11, 12, 13, 14, 15)
    ]
    assert rolls.tolist() == [1, 1, 3, 1, 1]  # Wednesday is charged three times


def test_a_closed_holiday_has_no_rollover() -> None:
    rolls = model().rollovers(
        pd.Timestamp("2024-03-25", tz="UTC"), pd.Timestamp("2024-03-31", tz="UTC")
    )
    days = [t.tz_convert("America/New_York").date() for t in rolls.index]
    assert date(2024, 3, 29) not in days  # Good Friday
    assert date(2024, 3, 28) in days


def test_financing_by_side_and_multiplier() -> None:
    charges = model().financing_usd([1.0, 1.0, -1.0, 0.0], PRICE, [1, 3, 1, 1])
    # 200,000 * 6% / 360 = 33.33 per night long; 200,000 * 2% / 360 = 11.11 short
    np.testing.assert_allclose(
        charges, [200_000 * 0.06 / 360, 200_000 * 0.18 / 360, 200_000 * 0.02 / 360, 0.0]
    )
    # broker terms (a non-provisional model) may credit a side: a negative rate is a credit
    data = CFG.cost_model_config().model_dump()
    data["provisional"] = False
    data["financing"]["short_rate_annual_pct"] = -1.0
    credit = CostModel(
        CostModelConfig.model_validate(data), CFG.instrument("xauusd"), CFG.sessions_config()
    )
    assert credit.financing_usd([-1.0], PRICE, [1])[0] < 0


@pytest.mark.parametrize(
    "rates",
    [
        {"short_rate_annual_pct": -1.0},
        {"short_rate_annual_pct": 0.0},
        {"long_rate_annual_pct": 0.0},
    ],
)
def test_a_provisional_model_charges_financing_on_both_sides(rates: dict[str, float]) -> None:
    # ADR 0032: until broker terms replace the placeholder, financing is a cost long and short.
    changes = {f"financing__{key}": value for key, value in rates.items()}
    with pytest.raises(ValidationError, match="financing on longs and shorts"):
        model(**changes)
    placeholder = CFG.cost_model_config().financing
    assert placeholder.long_rate_annual_pct > 0
    assert placeholder.short_rate_annual_pct > 0


def test_net_results_of_a_provisional_model_are_labelled_screening() -> None:
    assert model().result_label == SCREENING_LABEL == "screening, placeholder costs"
    data = CFG.cost_model_config().model_dump()
    data.update(provisional=False, venue="broker")
    real = CostModel(
        CostModelConfig.model_validate(data), CFG.instrument("xauusd"), CFG.sessions_config()
    )
    assert real.result_label == "net of broker costs"


def test_fallback_spread_from_hour_of_week_statistics() -> None:
    stats = pd.DataFrame(
        {"hour_of_week": np.arange(168), "p50": 0.2, "p90": 0.3, "p99": 0.5, "n": 100}
    )
    stats.loc[stats["hour_of_week"] == 24 + 16, "p90"] = 0.9  # Tuesday 16:00 New York
    with_stats = CostModel(
        CFG.cost_model_config(), CFG.instrument("xauusd"), CFG.sessions_config(), stats
    )
    spreads = with_stats.fallback_spread(utc("2024-03-12 20:30", "2024-03-12 15:00"))
    np.testing.assert_allclose(spreads, [0.9, 0.3])
    with pytest.raises(NoSpreadDataError, match="hours of week"):
        CostModel(
            CFG.cost_model_config(),
            CFG.instrument("xauusd"),
            CFG.sessions_config(),
            stats.iloc[:10],
        ).fallback_spread(utc("2024-03-12 20:30"))
    with pytest.raises(NoSpreadDataError):
        model().fallback_spread(utc("2024-03-12 20:30"))


def test_configuration_is_validated() -> None:
    with pytest.raises(ConfigError, match="not a session or event window"):
        model(slippage__multipliers={"lunch": 2.0})
    with pytest.raises(ValidationError, match="at least 1"):
        model(slippage__multipliers={"rollover_window": 0.5})
    with pytest.raises(ValidationError):
        model(slippage__fixed_bps=-1.0)
    with pytest.raises(ConfigError, match="cost_model 'missing'"):
        load_config("research", {"backtest.cost_model": "missing"}, config_dir=REPO / "config")
