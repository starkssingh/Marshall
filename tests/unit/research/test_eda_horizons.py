"""EDA-006: TGT-002 holding periods from 1m bars, round-trip costs from the cost model, the
cost-to-volatility table, the admission list and its guarded copy into a configuration directory
(temporary directories only: Sprint 5 writes no values to the repository's config/horizons.yaml,
ADR 0035)."""

import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from helpers.pipeline import REPO, config
from helpers.simulate import minute_bars
from xq.backtest.costs import SCREENING_LABEL, CostModel
from xq.cli.main import app
from xq.core.config import CostModelConfig, FinancingConfig, SlippageConfig
from xq.core.errors import ConfigError
from xq.data.calendar import MarketClock, regular_trading_day
from xq.research.eda.data import market_clock
from xq.research.eda.horizons import (
    ADMISSION_FILE,
    ALL_SESSIONS,
    AdmissionError,
    admission,
    admission_yaml,
    cost_to_volatility_figure,
    cost_to_volatility_table,
    decision_times,
    holding_periods,
    minute_quotes,
    period_costs,
    rollover_weights,
    trailing_rms,
    write_admission,
)
from xq.research.reports import MANIFEST_FILE, RUN_FILE
from xq.targets.returns import ForwardReturnParams

FIRST, LAST = date(2024, 3, 11), date(2024, 3, 14)  # Monday to Thursday, EDT
STEP = pd.Timedelta(minutes=5)
PARAMS = ForwardReturnParams(execution_latency_ms=1000, max_fill_delay_s=300, sigma_span_bars=96)
DRIFT = 1e-4  # the log mid rises 1 bp per market minute


def clock() -> MarketClock:
    """Like the EDA's: a trading day of margin either side of the data."""
    return market_clock(
        config(REPO).sessions_config(),
        pd.Timestamp("2024-03-10T22:00Z"),
        pd.Timestamp("2024-03-14T21:00Z"),
    )


def drifting_bars(spread_bps: float = 1.0) -> pd.DataFrame:
    sessions = config(REPO).sessions_config()
    count = len(minute_bars(sessions, FIRST, LAST))
    return minute_bars(
        sessions, FIRST, LAST, log_mid=DRIFT * np.arange(count), spread_bps=spread_bps
    )


def periods_of(bars: pd.DataFrame, label: str) -> pd.DataFrame:
    return holding_periods(
        bars,
        label,
        trading_day=regular_trading_day(config(REPO).sessions_config()),
        params=PARAMS,
        step=STEP,
        clock=clock(),
    )


def test_minute_quotes_are_bar_closes_stamped_inside_the_bar() -> None:
    bars = drifting_bars(spread_bps=2.0)
    quotes = minute_quotes(bars)
    assert (quotes["ts_utc"] < bars["available_at_utc"]).all()
    assert (quotes["ts_utc"] >= bars["available_at_utc"] - pd.Timedelta(minutes=1)).all()
    np.testing.assert_allclose((quotes["bid"] + quotes["ask"]) / 2, bars["close"])
    np.testing.assert_allclose(quotes["ask"] - quotes["bid"], bars["spread_close"])
    bid_bars = bars.assign(close=bars["close"] - bars["spread_close"] / 2)
    np.testing.assert_allclose(minute_quotes(bid_bars, "bid")["mid"], bars["close"])


def test_spreads_are_quoted_at_the_fills_not_bar_means() -> None:
    # The closing spread rises through the day (1 bp at the first bar, 1 bp more per market hour)
    # while the mean spread is ten times larger everywhere: the cost must use the closing spreads
    # of the entry and exit fills' bars, half each.
    bars = drifting_bars()
    hours = np.arange(len(bars)) / 60.0
    bars["spread_close"] = bars["close"] * (1.0 + hours) / 1e4
    bars["spread_mean"] = bars["spread_close"] * 10
    periods = periods_of(bars, "1h")
    quotes = minute_quotes(bars)
    stamps = quotes["ts_utc"].to_numpy()
    entry = np.searchsorted(stamps, periods["label_start"].to_numpy())
    exit_ = np.searchsorted(stamps, periods["label_end"].to_numpy())
    np.testing.assert_allclose(periods["entry_spread"], bars["spread_close"].to_numpy()[entry])
    np.testing.assert_allclose(periods["exit_spread"], bars["spread_close"].to_numpy()[exit_])
    costs = period_costs(periods, cost_model(), minutes(), sigma_minutes=60)
    expected = 0.5 * (1.0 + hours[entry]) + 0.5 * (1.0 + hours[exit_])  # bps of each mid
    np.testing.assert_allclose(costs["spread_bps"], expected, rtol=1e-9)


def test_decisions_are_the_bars_on_the_grid() -> None:
    bars = drifting_bars()
    decisions = decision_times(bars, STEP)
    assert len(decisions)
    assert (decisions.as_unit("ns").asi8 % STEP.value == 0).all()
    assert len(decisions) == int((bars["available_at_utc"].dt.minute % 5 == 0).sum())


def test_moves_are_trading_time_across_the_close() -> None:
    # One bp per market minute: every 15-minute period moves 15 bp, also when it holds over the
    # 17:00 close and the daily break; a trading day (1d) is 23 market hours = 1,380 bp.
    bars = drifting_bars()
    periods = periods_of(bars, "15m")
    np.testing.assert_allclose(periods["move"], 15 * DRIFT, rtol=1e-9)
    across = periods.loc[periods["crosses_close"]]
    assert len(across)
    decisions_ny = across.index.tz_convert("America/New_York")
    assert ((decisions_ny.hour == 16) & (decisions_ny.minute >= 45)).all()
    day = periods_of(bars, "1d")
    np.testing.assert_allclose(day["move"], 23 * 60 * DRIFT, rtol=1e-9)
    assert day["crosses_close"].all()


def test_closed_market_decisions_and_late_fills_are_not_measured() -> None:
    bars = drifting_bars()
    periods = periods_of(bars, "15m")
    decisions = decision_times(bars, STEP)
    at_close = decisions[decisions.tz_convert("America/New_York").hour == 17]
    assert len(at_close)  # the bar ending at the 17:00 close is available at the close ...
    assert not periods.index.isin(at_close).any()  # ... where the market is closed: no period
    assert periods["label_end"].max() <= bars["available_at_utc"].max()  # never beyond the data
    # A 20-minute data gap: fills more than 300 s late are missing, so the decisions whose entry
    # or exit would fall early in the gap are not measured; the others still move 15 bp.
    gap_start, gap_end = pd.Timestamp("2024-03-12T14:00Z"), pd.Timestamp("2024-03-12T14:20Z")
    inside = (bars["bar_start_utc"] >= gap_start) & (bars["bar_start_utc"] < gap_end)
    gapped = periods_of(bars.loc[~inside].reset_index(drop=True), "15m")
    assert len(periods.index.difference(gapped.index)) > 0
    for column in ("label_start", "label_end"):
        assert not ((gapped[column] >= gap_start) & (gapped[column] < gap_end)).any()
    before = gapped.loc[gapped["label_end"] < gap_start, "move"]
    np.testing.assert_allclose(before, 15 * DRIFT, rtol=1e-9)


def minutes() -> pd.DataFrame:
    ends = pd.date_range("2024-03-12T00:01:00", periods=2000, freq="1min", tz="UTC")
    return pd.DataFrame({"ret_end": ends, "ret": np.full(2000, 2e-4)})  # 2 bps a minute


def test_trailing_rms() -> None:
    ends = np.array([10, 20, 30, 40], dtype=np.int64)
    values = np.array([1.0, 2.0, 3.0, 4.0])
    rms = trailing_rms(ends, values, np.array([5, 20, 39, 100], dtype=np.int64), 2)
    assert np.isnan(rms[0])
    np.testing.assert_allclose(rms[1:], [np.sqrt(2.5), np.sqrt(6.5), np.sqrt(12.5)])


def cost_model() -> CostModel:
    return CostModel.from_config(config(REPO), "xauusd")


def test_rollover_weights_count_the_triple_day() -> None:
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
    weights = rollover_weights(cost_model(), starts.as_unit("ns").asi8, ends.as_unit("ns").asi8)
    np.testing.assert_allclose(weights, [1.0, 3.0, 0.0, 1.0])  # [start, end): 21:00 is inside


def two_periods() -> pd.DataFrame:
    entry = pd.to_datetime(["2024-03-12T12:00:00", "2024-03-12T20:00:00"], utc=True)
    return pd.DataFrame(
        {
            "label_start": entry,
            "label_end": entry + pd.Timedelta(hours=2),
            "crosses_close": [False, True],
            "move": [10e-4, -20e-4],
            "entry_mid": 2000.0,
            "exit_mid": [2002.0, 1996.0],
            "entry_spread": 0.2,
            "exit_spread": 0.4,
        },
        index=pd.DatetimeIndex(entry - pd.Timedelta(seconds=1), name="decision_time"),
    )


def test_period_costs_components() -> None:
    out = period_costs(two_periods(), cost_model(), minutes(), sigma_minutes=60)
    np.testing.assert_allclose(out["move_bps"], [10.0, 20.0])
    half_spreads = 0.5 * 0.2 / 2000.0 + 0.5 * 0.4 / np.array([2002.0, 1996.0])
    np.testing.assert_allclose(out["spread_bps"], half_spreads * 1e4)
    np.testing.assert_allclose(out["commission_bps"], 2 * 3.5 / (100 * 2000.0) * 1e4)
    # One rollover at the mean of the long (6 %) and short (2 %) rates, act/360.
    np.testing.assert_allclose(out["financing_bps"], [0.0, 4.0 / 100 / 360 * 1e4])
    # Slippage: 0.5 + 0.1 * 2 bps (sigma-hat of 2 bps a minute) per fill. The second period
    # enters at 16:00 New York (outside the rollover window) and exits at 18:00 (inside: x 3).
    assert out["slippage_bps"].iloc[0] == pytest.approx(2 * 0.7)
    assert out["slippage_bps"].iloc[1] == pytest.approx(0.7 + 3 * 0.7)
    total = out[["spread_bps", "commission_bps", "slippage_bps", "financing_bps"]].sum(axis=1)
    np.testing.assert_allclose(out["cost_bps"], total)


def test_cost_table_and_admission() -> None:
    bars = drifting_bars()
    minute_returns = pd.DataFrame(
        {"ret_end": bars["available_at_utc"], "ret": np.full(len(bars), DRIFT)}
    )
    periods = {label: periods_of(bars, label) for label in ("5m", "4h")}
    table = cost_to_volatility_table(
        periods,
        minute_returns,
        cost_model(),
        config(REPO).sessions_config(),
        max_cost_to_vol=0.3,
        sigma_minutes=60,
    )
    overall = table.loc[table["session"] == ALL_SESSIONS].set_index("horizon")
    assert not overall.loc["5m", "admitted"]  # 5 bp against about 3 bp of costs
    assert overall.loc["4h", "admitted"]  # 240 bp against about 3 bp
    assert overall.loc["4h", "move_bps"] == pytest.approx(240.0)
    assert overall.loc["4h", "cost_to_vol"] == pytest.approx(overall.loc["4h", "cost_bps"] / 240.0)
    assert overall.loc["5m", "n"] == len(periods["5m"])
    assert 0 < overall.loc["4h", "crosses_close_share"] < 1
    assert set(table["cost_basis"]) == {SCREENING_LABEL}
    assert {"london", "new_york", "london_new_york"} <= set(table["session"])
    result = admission(table, ["15m", "5m", "4h"], 0.3, provisional_costs=True)
    assert result.admitted == ["4h"]
    assert result.excluded == ["15m", "5m"]  # no data for 15m: not shown to be affordable
    assert result.by_session["london"] == ["4h"]
    assert result.cost_basis == SCREENING_LABEL
    loaded = yaml.safe_load(admission_yaml(result, {"dataset_id": "ds-x"}))
    assert loaded["admitted"] == ["4h"]
    assert loaded["provenance"] == {"dataset_id": "ds-x"}
    assert (loaded["cost_basis"], loaded["provisional_costs"]) == (SCREENING_LABEL, True)
    assert len(cost_to_volatility_figure(table, 0.3, "t").axes) == 1


SIGMA = 2e-4  # log mid volatility per market minute (2 bp)
RW_FIRST, RW_LAST = date(2024, 1, 8), date(2024, 7, 5)  # 26 weeks of trading days


def spread_only_cost() -> CostModel:
    """A cost model whose only cost is the quoted spread (paid through the fills)."""
    cfg = config(REPO)
    terms = CostModelConfig(
        venue="analytic",
        provisional=False,
        latency_ms=1000,
        max_fill_delay_s=300,
        slippage=SlippageConfig(fixed_bps=0.0, sigma_multiple=0.0),
        financing=FinancingConfig(long_rate_annual_pct=0.0, short_rate_annual_pct=0.0),
    )
    return CostModel(terms, cfg.instrument("xauusd"), cfg.sessions_config())


def random_walk_table(spread_bps: float) -> pd.DataFrame:
    sessions = config(REPO).sessions_config()
    count = len(minute_bars(sessions, RW_FIRST, RW_LAST))
    steps = np.random.default_rng(20240108).standard_normal(count) * SIGMA
    bars = minute_bars(sessions, RW_FIRST, RW_LAST, log_mid=np.cumsum(steps), spread_bps=spread_bps)
    minute_returns = pd.DataFrame({"ret_end": bars["available_at_utc"], "ret": steps})
    clock_ = market_clock(sessions, bars["bar_start_utc"].min(), bars["available_at_utc"].max())
    periods = {
        label: holding_periods(
            bars,
            label,
            trading_day=regular_trading_day(sessions),
            params=PARAMS,
            step=STEP,
            clock=clock_,
        )
        for label in ("15m", "1h")
    }
    return cost_to_volatility_table(
        periods,
        minute_returns,
        spread_only_cost(),
        sessions,
        max_cost_to_vol=0.3,
        sigma_minutes=60,
    )


def expected_move_bps(minutes_: float) -> float:
    """Mean |N(0, sigma^2 h)| in bps: sigma * sqrt(2 h / pi)."""
    return SIGMA * np.sqrt(2 * minutes_ / np.pi) * 1e4


@pytest.mark.parametrize(("target_ratio", "admitted"), [(0.27, True), (0.33, False)])
def test_gaussian_random_walk_matches_theory(target_ratio: float, admitted: bool) -> None:
    # A random walk with 2 bp per market minute and a constant spread s (bps of the mid): the
    # mean absolute move over h market minutes is sigma * sqrt(2 h / pi) and the round-trip cost
    # is s (half at entry, half at exit), so the ratio is s / (sigma * sqrt(2 h / pi)). The spread
    # puts the 1h ratio 10 % below or above the 0.3 bound; 15m is twice as dear either way.
    spread_bps = target_ratio * expected_move_bps(60)
    table = random_walk_table(spread_bps).set_index(["horizon", "session"])
    for label, minutes_ in (("15m", 15), ("1h", 60)):
        row = table.loc[(label, ALL_SESSIONS)]
        assert row["n"] > 30_000
        assert row["move_bps"] == pytest.approx(expected_move_bps(minutes_), rel=0.03)
        assert row["cost_bps"] == pytest.approx(spread_bps, rel=1e-9)
        analytic = spread_bps / expected_move_bps(minutes_)
        assert row["cost_to_vol"] == pytest.approx(analytic, rel=0.03)
    result = admission(table.reset_index(), ["15m", "1h"], 0.3, provisional_costs=False)
    assert result.admitted == (["1h"] if admitted else [])
    assert "15m" in result.excluded


def test_the_target_set_must_be_a_configured_forward_return_set() -> None:
    with pytest.raises(ConfigError, match="forward_return target set"):
        config(REPO, **{"eda.horizons.target_set.version": "v9"})
    with pytest.raises(ConfigError, match="mixes days"):
        config(REPO, **{"eda.horizons.candidates": ["1d12h"]})


def report(directory: Path, *, confirmatory: bool, provisional: bool | None = False) -> Path:
    """A minimal EDA report holding an admission list (`provisional` None: not recorded)."""
    directory.mkdir(parents=True)
    listed: dict[str, object] = {"admitted": ["4h"], "cost_basis": "net of broker costs"}
    if provisional is not None:
        listed["provisional_costs"] = provisional
        if provisional:
            listed["cost_basis"] = SCREENING_LABEL
    text = yaml.safe_dump(listed, sort_keys=False)
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
    written = yaml.safe_load(target.read_text())
    assert written["admitted"] == ["4h"]
    assert (written["cost_basis"], written["provisional_costs"]) == ("net of broker costs", False)
    assert written["allow_placeholder_costs"] is False
    assert written["source"] == {"report": str(good), "run_id": "01RUN"}


def test_placeholder_costs_need_an_explicit_flag(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    placeholder = report(tmp_path / "placeholder", confirmatory=True, provisional=True)
    with pytest.raises(AdmissionError, match="--allow-placeholder-costs"):
        write_admission(placeholder, config_dir)
    unrecorded = report(tmp_path / "unrecorded", confirmatory=True, provisional=None)
    with pytest.raises(AdmissionError, match="provisional costs"):
        write_admission(unrecorded, config_dir)  # not saying counts as provisional
    assert not (config_dir / "horizons.yaml").exists()
    target = write_admission(placeholder, config_dir, allow_placeholder_costs=True)
    written = yaml.safe_load(target.read_text())
    assert written["cost_basis"] == SCREENING_LABEL
    assert written["provisional_costs"] is True
    assert written["allow_placeholder_costs"] is True


def test_cli_admit_horizons_flag(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    for path in (REPO / "config").rglob("*.yaml"):
        target = config_dir / path.relative_to(REPO / "config")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    placeholder = report(tmp_path / "placeholder", confirmatory=True, provisional=True)
    command = [
        "--config-dir",
        str(config_dir),
        "--set",
        f"paths.root={tmp_path}",
        "research",
        "admit-horizons",
        "--report",
        str(placeholder),
    ]
    refused = CliRunner().invoke(app, command)
    assert refused.exit_code == 2
    assert "--allow-placeholder-costs" in refused.output
    assert not (config_dir / "horizons.yaml").exists()
    allowed = CliRunner().invoke(app, [*command, "--allow-placeholder-costs"])
    assert allowed.exit_code == 0, allowed.output
    assert yaml.safe_load((config_dir / "horizons.yaml").read_text())["allow_placeholder_costs"]
