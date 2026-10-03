"""EXP-004: every evaluated configuration is counted; near-duplicate trials count once, and so
does a rule and its mirror image (absolute correlation, C-25, ADR 0058)."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from helpers.pipeline import REPO, config
from xq.cli.main import app
from xq.core.config import AppConfig, TrialClusteringConfig
from xq.core.errors import NaiveTimestampError
from xq.tracking import registry
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.runs import experiment_run
from xq.tracking.trials import cluster_trials, daily_returns, effective_trials, trial_count

PARAMS = TrialClusteringConfig(correlation_threshold=0.7, min_common_days=60)
INDEX = pd.date_range("2024-01-01 22:00", periods=100 * 24, freq="1h", tz="UTC")  # 100 days


def returns(seed: int, base: np.ndarray | None = None, noise: float = 1.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    values = rng.normal(0, noise, len(INDEX))
    if base is not None:
        values = base + values
    return pd.Series(values, index=INDEX)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(
        engine, "H-0001", title="t", family_id="momentum", yaml_text="x\n"
    )
    yield engine
    engine.dispose()


def test_near_duplicates_count_once() -> None:
    common = np.random.default_rng(0).normal(0, 1, len(INDEX))
    series = {
        "a": returns(1, common, 0.1),
        "b": returns(2, common, 0.1),
        "c": returns(3, common, 0.1),
        "d": returns(4),
        "e": returns(5),
        "f": returns(6),
    }
    assert effective_trials(series, PARAMS) == 4  # one cluster of three plus three singletons
    assert effective_trials({}, PARAMS) == 0
    assert effective_trials({"a": series["a"]}, PARAMS) == 1


def rules_and_mirrors() -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    """Five rules (two near-duplicates) net of costs, and their mirror images: the same signals
    traded the other way, which pay the same costs."""
    common = np.random.default_rng(0).normal(0, 1, len(INDEX))
    gross = {
        "trend_10": returns(1, common, 0.1),
        "trend_12": returns(2, common, 0.1),
        "carry": returns(3),
        "value": returns(4),
        "breakout": returns(5),
    }
    costs = {name: 0.02 * (i + 1) for i, name in enumerate(gross)}
    rules = {name: r - costs[name] for name, r in gross.items()}
    mirrors = {f"{name}~mirror": -r - costs[name] for name, r in gross.items()}
    return rules, mirrors


def test_a_rule_and_its_mirror_image_are_one_trial() -> None:
    rules, mirrors = rules_and_mirrors()
    alone = cluster_trials(list(rules.values()), PARAMS)
    both = cluster_trials([*rules.values(), *mirrors.values()], PARAMS)
    assert alone.n_clusters == both.n_clusters == 4  # the trend pair, and three singletons
    assert effective_trials({**rules, **mirrors}, PARAMS) == 4
    # each mirror sits in its rule's cluster, trading the other way
    assert both.labels[5:] == both.labels[:5]
    assert both.signs[:5] == alone.signs == (1.0,) * 5
    assert both.signs[5:] == (-1.0,) * 5
    # a mirror adds nothing to the variance: its Sharpe ratio is left out, not negated (negating
    # a net return would count its costs as income)
    sharpes = [0.4, 0.5, -0.3, 0.8, -1.2]
    mirrored = [-0.6, -0.7, 0.1, -1.0, 1.0]
    assert both.sharpe_variance([*sharpes, *mirrored]) == alone.sharpe_variance(sharpes)
    assert alone.sharpe_variance(sharpes) == pytest.approx(np.var([0.45, -0.3, 0.8, -1.2], ddof=1))


def test_a_family_with_mirrors_counts_like_the_rules_alone(cfg: AppConfig, engine: Engine) -> None:
    """The registry's counts (C-25 (2)): a family holding each rule and its mirror image has the
    same effective trial count and Sharpe variance as the rules alone."""
    rules, mirrors = rules_and_mirrors()
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        for family, members in (("rules", rules), ("mirrored", {**rules, **mirrors})):
            for name, r in members.items():
                daily = daily_returns(r)
                run.record_trial(
                    family_id=family,
                    config={"rule": name},
                    evaluated_on_test=True,
                    sharpe=float(daily.mean() / daily.std() * np.sqrt(252)),
                    returns=r,
                )
    alone, both = trial_count(cfg, engine, "rules"), trial_count(cfg, engine, "mirrored")
    assert (alone.n_trials, both.n_trials) == (5, 10)
    assert alone.effective_n == both.effective_n == 4
    assert alone.sharpe_variance is not None
    assert both.sharpe_variance == pytest.approx(alone.sharpe_variance)


def test_too_little_overlap_counts_as_independent() -> None:
    a = returns(1)
    b = a.iloc[: 59 * 24].copy()  # identical values, but only 59 common trading days
    assert effective_trials({"a": a, "b": b}, PARAMS) == 2
    assert effective_trials({"a": a, "b": a.iloc[: 60 * 24].copy()}, PARAMS) == 1
    assert effective_trials({"a": a, "b": a.copy()}, PARAMS) == 1


def test_returns_are_compared_as_trading_day_sums() -> None:
    hourly = returns(1)
    daily = daily_returns(hourly)
    # 22:00 UTC is the 17:00 New York roll in winter: the first 24 hours are one trading day.
    assert daily.index[0] == pd.Timestamp("2024-01-02")
    assert daily.iloc[0] == pytest.approx(hourly.iloc[:24].sum())
    assert daily.sum() == pytest.approx(hourly.sum())
    # The same strategy sampled daily and hourly is one trial, not two.
    stamps = pd.DatetimeIndex(
        [INDEX[0] + pd.Timedelta(hours=12) + pd.Timedelta(days=i) for i in range(len(daily))]
    )
    assert (
        effective_trials(
            {"hourly": hourly, "daily": pd.Series(daily.to_numpy(), index=stamps)}, PARAMS
        )
        == 1
    )
    # Hourly noise that cancels within each day does not dilute the correlation.
    wiggle = np.tile([1.0, -1.0], len(INDEX) // 2) * 5
    assert effective_trials({"a": hourly, "b": hourly + wiggle}, PARAMS) == 1
    with pytest.raises(NaiveTimestampError):
        daily_returns(pd.Series([0.1], index=pd.DatetimeIndex(["2024-01-01"])))


def test_every_trial_is_counted_per_family_and_globally(cfg: AppConfig, engine: Engine) -> None:
    common = np.random.default_rng(0).normal(0, 1, len(INDEX))
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        for i in range(3):
            run.record_trial(
                family_id="momentum",
                config={"lookback": 10 + i},
                evaluated_on_test=True,
                sharpe=0.1 * i,
                returns=returns(i, common, 0.1),
            )
        run.record_trial(
            family_id="momentum", config={"lookback": 99}, evaluated_on_test=False, sharpe=0.5
        )
        run.record_trial(
            family_id="carry", config={"x": 1}, evaluated_on_test=True, returns=returns(9)
        )
    momentum = trial_count(cfg, engine, "momentum")
    assert momentum.n_trials == 4
    assert momentum.n_test_evaluations == 3
    assert momentum.effective_n == 2  # three near-duplicates, plus one trial without returns
    # the variance is across clusters (C-25): the near-duplicates' mean Sharpe ratio (0.1) and the
    # trial without returns (0.5), not the four raw trials
    assert momentum.sharpe_variance == pytest.approx(np.var([0.1, 0.5], ddof=1))
    everything = trial_count(cfg, engine)
    assert (everything.family_id, everything.n_trials, everything.effective_n) == (None, 5, 3)
    assert trial_count(cfg, engine, "none").n_trials == 0


def test_trials_belong_to_a_live_run(cfg: AppConfig, engine: Engine) -> None:
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        pass
    with pytest.raises(registry.RegistryError, match="not running"):
        run.record_trial(family_id="momentum", config={}, evaluated_on_test=True)
    with (
        experiment_run(cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True) as r,
        pytest.raises(NaiveTimestampError),
    ):
        r.record_trial(
            family_id="momentum",
            config={},
            evaluated_on_test=True,
            returns=pd.Series([0.1], index=pd.DatetimeIndex(["2024-01-01"])),
        )


def test_cli_trials(cfg: AppConfig, engine: Engine) -> None:
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        run.record_trial(family_id="momentum", config={}, evaluated_on_test=True, sharpe=0.2)
    result = CliRunner().invoke(
        app,
        [
            "--config-dir",
            str(REPO / "config"),
            "--set",
            f"paths.root={cfg.paths.root}",
            "--set",
            f"paths.migrations_dir={REPO / 'migrations'}",
            "--set",
            "logging.console=false",
            "exp",
            "trials",
            "--family",
            "momentum",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "family momentum: 1 trial(s), 1 evaluated on test folds, 1 effectively" in result.stdout
