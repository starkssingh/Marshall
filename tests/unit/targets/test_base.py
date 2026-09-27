"""TGT-001: target specs, long target frames and the schema guard."""

from datetime import date, time

import pandas as pd
import pytest

from helpers.pipeline import REPO
from helpers.targets import STUB_DEFINITION as DEFINITION
from helpers.targets import STUB_KIND as KIND
from helpers.targets import TRADING_DAY, stub_compute
from xq.core.config import TargetSetConfig, load_config
from xq.data.calendar import MarketClock, regular_trading_day
from xq.targets.base import (
    TargetKind,
    TargetLeakError,
    TargetSpec,
    check_feature_matrix,
    compute_targets,
    definition_hash,
    fill_delay_report,
    market_horizon,
    target_values,
)

T = pd.date_range("2024-03-12 10:00", periods=4, freq="15min", tz="UTC", name="decision_time")
CLOCK = MarketClock.for_range(
    load_config("research", config_dir=REPO / "config").sessions_config(),
    date(2024, 3, 11),
    date(2024, 3, 13),
)


def test_long_frame_has_one_row_per_decision_and_target() -> None:
    specs = KIND.expand(DEFINITION, TRADING_DAY)
    frame = compute_targets(KIND, specs, pd.DataFrame(), pd.Series(1.0, index=T), CLOCK)
    assert frame.index.name == "decision_time"
    assert list(frame.columns) == [
        "target",
        "value",
        "label_start",
        "label_end",
        "crosses_close",
        "scale",
        "fill_delay_s",
    ]
    assert len(frame) == len(T) * 4
    assert frame["target"].iloc[:4].tolist() == sorted(s.name for s in specs)
    one = target_values(frame, "stub_long_1h")
    assert one.index.equals(T)
    assert (one["label_end"] - one["label_start"] == pd.Timedelta("1h")).all()
    with pytest.raises(KeyError, match="available"):
        target_values(frame, "missing")


def test_kinds_must_return_every_value_column() -> None:
    def bad(
        spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series, clock: MarketClock
    ) -> pd.DataFrame:
        return stub_compute(spec, quotes, sigma, clock).drop(columns="label_end")

    broken = TargetKind("bad", 1, KIND.expand, KIND.sigma, bad, KIND.lookahead)
    with pytest.raises(ValueError, match="label_end"):
        compute_targets(
            broken,
            KIND.expand(DEFINITION, TRADING_DAY),
            pd.DataFrame(),
            pd.Series(1.0, index=T),
            CLOCK,
        )


@pytest.mark.parametrize("column", ["stub_long_1h", "label_end", "value", "tgt_anything", "fwd_x"])
def test_schema_guard_rejects_target_columns(column: str) -> None:
    features = pd.DataFrame({"close": [1.0], column: [2.0]})
    with pytest.raises(TargetLeakError, match=column):
        check_feature_matrix(features, ["stub_long_1h"])


def test_schema_guard_accepts_ordinary_features() -> None:
    check_feature_matrix(pd.DataFrame({"close": [1.0], "ctx_1h_close": [1.0]}), ["stub_long_1h"])


def test_definition_hash_and_validation() -> None:
    same = TargetSetConfig(kind="stub", horizons=["15m", "1h"], price_refs=["long", "mid"])
    assert definition_hash(same) == definition_hash(DEFINITION)
    other = TargetSetConfig(kind="stub", horizons=["15m"], price_refs=["long", "mid"])
    assert definition_hash(other) != definition_hash(DEFINITION)
    with pytest.raises(ValueError, match="positive"):
        TargetSetConfig(kind="stub", horizons=["0s"], price_refs=["long"])
    with pytest.raises(ValueError, match="unique"):
        TargetSetConfig(kind="stub", horizons=["1h", "1h"], price_refs=["long"])
    with pytest.raises(ValueError, match="invalid horizon"):
        TargetSetConfig(kind="stub", horizons=["soon"], price_refs=["long"])


def test_fill_delay_report_counts_labelled_rows_with_a_late_fill() -> None:
    frame = pd.DataFrame(
        {
            "target": ["a", "a", "a", "a", "b"],
            "value": [0.1, 0.2, float("nan"), 0.3, float("nan")],
            "fill_delay_s": [0.4, 7.25, float("nan"), 5.0, float("nan")],
        }
    )
    assert fill_delay_report(frame, 5.0) == {
        "a": {"labelled": 3, "delayed": 1, "max_delay_s": 7.25},  # 5.0 is not more than 5 s
        "b": {"labelled": 0, "delayed": 0, "max_delay_s": None},
    }


def test_market_horizon_counts_days_as_trading_days() -> None:
    day = pd.Timedelta(hours=23)
    assert market_horizon("1d", day) == pd.Timedelta(hours=23)
    assert market_horizon("2D", day) == pd.Timedelta(hours=46)
    assert market_horizon("4h", day) == pd.Timedelta(hours=4)
    assert market_horizon("36h", day) == pd.Timedelta(hours=36)
    assert market_horizon("15m", day) == pd.Timedelta(minutes=15)


@pytest.mark.parametrize("label", ["1d6h", "1 days", "2day"])
def test_horizon_labels_may_not_mix_days_with_other_units(label: str) -> None:
    with pytest.raises(ValueError, match="mixes days"):
        TargetSetConfig(kind="stub", horizons=[label], price_refs=["mid"])


def test_regular_trading_day_follows_the_configured_market_hours() -> None:
    sessions = load_config("research", config_dir=REPO / "config").sessions_config()
    assert regular_trading_day(sessions) == pd.Timedelta(hours=23)
    day_session = sessions.model_copy(
        update={"market": sessions.market.model_copy(update={"open": time(9), "close": time(17)})}
    )
    assert regular_trading_day(day_session) == pd.Timedelta(hours=8)
