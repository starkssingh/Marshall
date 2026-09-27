"""EDA-006: round-trip costs from the cost model, the cost-to-volatility table, the admission list
and its guarded copy into a configuration directory (temporary directories only: Sprint 5 writes
no values to the repository's config/horizons.yaml, ADR 0035)."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from helpers.pipeline import REPO, config
from xq.backtest.costs import SCREENING_LABEL, CostModel
from xq.research.eda.horizons import (
    ADMISSION_FILE,
    ALL_SESSIONS,
    AdmissionError,
    admission,
    admission_yaml,
    cost_to_volatility_figure,
    cost_to_volatility_table,
    period_costs,
    rollover_weights,
    trailing_rms,
    write_admission,
)
from xq.research.reports import MANIFEST_FILE, RUN_FILE


def cost_model() -> CostModel:
    return CostModel.from_config(config(REPO), "xauusd")


def periods(starts: list[str], hours: float, moves_bps: list[float]) -> pd.DataFrame:
    start = pd.to_datetime(starts, utc=True)
    return pd.DataFrame(
        {
            "bar_start": start,
            "ret_start": start,
            "ret_end": start + pd.Timedelta(hours=hours),
            "price": 2000.0,
            "ret": np.asarray(moves_bps) / 1e4,
            "spread_bps": 1.0,
        }
    )


def minutes() -> pd.DataFrame:
    ends = pd.date_range("2024-03-12T00:01:00", periods=2000, freq="1min", tz="UTC")
    return pd.DataFrame({"ret_end": ends, "ret": np.full(2000, 2e-4)})  # 2 bps a minute


def test_trailing_rms() -> None:
    ends = np.array([10, 20, 30, 40], dtype=np.int64)
    values = np.array([1.0, 2.0, 3.0, 4.0])
    rms = trailing_rms(ends, values, np.array([5, 20, 39, 100], dtype=np.int64), 2)
    assert np.isnan(rms[0])
    np.testing.assert_allclose(rms[1:], [np.sqrt(2.5), np.sqrt(6.5), np.sqrt(12.5)])


def test_rollover_weights_count_the_triple_day() -> None:
    cost = cost_model()
    # Tuesday 12 and Wednesday 13 March 2024: rollovers at 17:00 New York = 21:00 UTC.
    starts = pd.to_datetime(
        [
            "2024-03-12T20:00:00",
            "2024-03-13T20:00:00",
            "2024-03-12T12:00:00",
            "2024-03-12T21:00:00",
        ],
        utc=True,
    )
    ends = starts + pd.Timedelta(hours=2)
    ns = starts.as_unit("ns").asi8
    weights = rollover_weights(cost, ns, ends.as_unit("ns").asi8)
    np.testing.assert_allclose(weights, [1.0, 3.0, 0.0, 1.0])  # [start, end): 21:00 is inside


def test_period_costs_components() -> None:
    cost = cost_model()
    frame = periods(["2024-03-12T12:00:00", "2024-03-12T20:00:00"], 2.0, [10.0, -20.0])
    out = period_costs(frame, cost, minutes(), sigma_minutes=60)
    np.testing.assert_allclose(out["move_bps"], [10.0, 20.0])
    np.testing.assert_allclose(out["spread_bps"], 1.0)
    np.testing.assert_allclose(out["commission_bps"], 2 * 3.5 / (100 * 2000.0) * 1e4)
    # One rollover at the mean of the long (6 %) and short (2 %) rates, act/360.
    np.testing.assert_allclose(out["financing_bps"], [0.0, 4.0 / 100 / 360 * 1e4])
    # Slippage: 0.5 + 0.1 * 2 bps (sigma-hat of 2 bps a minute) per side. The second period
    # enters at 16:00 New York (outside the rollover window) and exits at 17:59:59 (inside: x 3).
    assert out["slippage_bps"].iloc[0] == pytest.approx(2 * 0.7)
    assert out["slippage_bps"].iloc[1] == pytest.approx(0.7 + 3 * 0.7)
    total = out[["spread_bps", "commission_bps", "slippage_bps", "financing_bps"]].sum(axis=1)
    np.testing.assert_allclose(out["cost_bps"], total)


def test_cost_table_and_admission() -> None:
    cost = cost_model()
    sessions = config(REPO).sessions_config()
    starts = [f"2024-03-12T{h:02d}:00:00" for h in range(1, 16)]
    table = cost_to_volatility_table(
        {
            "1h": periods(starts, 1.0, [4.0] * len(starts)),  # cost of about 3 bps: too dear
            "4h": periods(starts, 4.0, [40.0] * len(starts)),  # about 0.08: admitted
        },
        minutes(),
        cost,
        sessions,
        max_cost_to_vol=0.3,
        sigma_minutes=60,
    )
    overall = table.loc[table["session"] == ALL_SESSIONS].set_index("horizon")
    assert not overall.loc["1h", "admitted"]
    assert overall.loc["4h", "admitted"]
    assert overall.loc["4h", "cost_to_vol"] == pytest.approx(overall.loc["4h", "cost_bps"] / 40.0)
    assert set(table["cost_basis"]) == {SCREENING_LABEL}
    assert {"london", "new_york", "london_new_york"} <= set(table["session"])
    result = admission(table, ["15m", "1h", "4h"], 0.3)
    assert result.admitted == ["4h"]
    assert result.excluded == ["15m", "1h"]  # no data for 15m: not shown to be affordable
    assert result.by_session["london"] == ["4h"]
    assert result.cost_basis == SCREENING_LABEL
    loaded = yaml.safe_load(admission_yaml(result, {"dataset_id": "ds-x"}))
    assert loaded["admitted"] == ["4h"]
    assert loaded["provenance"] == {"dataset_id": "ds-x"}
    assert len(cost_to_volatility_figure(table, 0.3, "t").axes) == 1


def report(directory: Path, *, confirmatory: bool) -> Path:
    directory.mkdir(parents=True)
    text = "admitted:\n- 4h\n"
    (directory / ADMISSION_FILE).write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    (directory / MANIFEST_FILE).write_text(json.dumps({"files": {ADMISSION_FILE: digest}}))
    (directory / RUN_FILE).write_text(json.dumps({"run_id": "01RUN", "confirmatory": confirmatory}))
    return directory


def test_write_admission_only_from_a_confirmatory_unaltered_report(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    exploratory = report(tmp_path / "exploratory", confirmatory=False)
    with pytest.raises(AdmissionError, match="exploratory"):
        write_admission(exploratory, config_dir)
    confirmatory = report(tmp_path / "confirmatory", confirmatory=True)
    (confirmatory / ADMISSION_FILE).write_text("admitted:\n- 1m\n")
    with pytest.raises(AdmissionError, match="differs from the report manifest"):
        write_admission(confirmatory, config_dir)
    with pytest.raises(AdmissionError, match="not an EDA report"):
        write_admission(tmp_path / "missing", config_dir)
    assert not (config_dir / "horizons.yaml").exists()
    good = report(tmp_path / "good", confirmatory=True)
    target = write_admission(good, config_dir)
    assert target == config_dir / "horizons.yaml"
    assert yaml.safe_load(target.read_text()) == {"admitted": ["4h"]}
    assert "run 01RUN" in target.read_text()
