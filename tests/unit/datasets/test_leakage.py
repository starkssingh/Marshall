"""DS-006: the leakage harness's own contract (inputs, outputs, reporting, determinism)."""

import numpy as np
import pandas as pd
import pytest

from xq.datasets.leakage import (
    Inputs,
    LeakageReport,
    Violation,
    check_feature_causality,
    correlation_scan,
)

T0 = pd.Timestamp("2024-03-12 10:00", tz="UTC")


def inputs(n: int = 60) -> dict[str, pd.DataFrame]:
    times = pd.date_range(T0, periods=n, freq="15min")
    values = np.cumsum(np.random.default_rng(1).normal(size=n)) + 100
    return {"base": pd.DataFrame({"available_at_utc": times, "close": values})}


def indexed(data: Inputs) -> pd.Series:
    frame = data["base"]
    return pd.Series(
        frame["close"].to_numpy(), index=pd.DatetimeIndex(frame["available_at_utc"], name="t")
    )


def test_a_causal_feature_passes_and_points_are_deterministic() -> None:
    def fn(data: Inputs) -> pd.DataFrame:
        return pd.DataFrame({"lagged": indexed(data).shift(1)})

    first = check_feature_causality(fn, inputs(), n_points=10, seed=5)
    second = check_feature_causality(fn, inputs(), n_points=10, seed=5)
    assert first.passed
    assert first.summary() == "no leakage found at 10 test points"
    assert first.points == second.points
    assert first.points[0] == T0
    assert first.points[-1] == T0 + pd.Timedelta(minutes=15 * 59)


def test_a_one_step_look_ahead_is_caught_at_its_first_instant() -> None:
    def fn(data: Inputs) -> pd.DataFrame:
        return pd.DataFrame({"peek": indexed(data).diff().shift(-1)})  # tomorrow's change

    report = check_feature_causality(fn, inputs(), n_points=10, seed=5)
    assert report.checks_failed == {"truncation", "perturbation"}
    truncation = next(v for v in report.violations if v.check == "truncation")
    assert truncation.column == "peek"
    assert truncation.at is not None
    assert "depends on data available only after" in truncation.detail


def test_changed_rows_are_reported() -> None:
    def fn(data: Inputs) -> pd.DataFrame:
        series = indexed(data)
        # Drops the last row only when it has the full data: the row set depends on the future.
        return pd.DataFrame({"x": series}).iloc[: max(len(series) - 1, 1)]

    report = check_feature_causality(fn, inputs(), n_points=10, seed=5)
    assert any(v.column == "<decision times>" for v in report.violations)


def test_malformed_inputs_and_outputs_are_usage_errors() -> None:
    with pytest.raises(ValueError, match="no availability column"):
        check_feature_causality(lambda d: pd.DataFrame(), {"base": pd.DataFrame({"x": [1]})})
    with pytest.raises(ValueError, match="tz-aware decision times"):
        check_feature_causality(lambda d: pd.DataFrame({"x": [1.0]}), inputs())

    def unsorted(data: Inputs) -> pd.DataFrame:
        return pd.DataFrame({"x": indexed(data)}).iloc[::-1]

    with pytest.raises(ValueError, match="increasing"):
        check_feature_causality(unsorted, inputs())


def test_report_keeps_the_earliest_violation_per_check_and_column() -> None:
    report = LeakageReport()
    report._add(Violation("truncation", "f", T0 + pd.Timedelta(hours=2), "late"))
    report._add(Violation("truncation", "f", T0, "early"))
    report._add(Violation("truncation", "g", T0, "other column"))
    assert [(v.column, v.detail) for v in report.violations] == [
        ("f", "early"),
        ("g", "other column"),
    ]
    assert "[truncation] f at" in report.summary()


def test_correlation_scan_ignores_short_or_constant_series() -> None:
    index = pd.date_range(T0, periods=10, freq="15min")
    x = pd.DataFrame({"f": np.arange(10.0)}, index=index)
    assert correlation_scan(x, x.rename(columns={"f": "y"})).passed  # fewer than 30 rows
    index = pd.date_range(T0, periods=40, freq="15min")
    flat = pd.DataFrame({"f": np.ones(40)}, index=index)
    y = pd.DataFrame({"y": np.arange(40.0)}, index=index)
    assert correlation_scan(flat, y).passed
    assert not correlation_scan(y.rename(columns={"y": "f"}), y).passed
