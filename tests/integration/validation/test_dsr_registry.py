"""VAL-002: the DSR reads the trial count and Sharpe variance registered for the family."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from helpers.pipeline import REPO, config
from xq.tracking import registry
from xq.tracking.db import create_db_engine, upgrade_to_head
from xq.tracking.runs import experiment_run
from xq.validation.dsr import deflated_sharpe_for_family, deflated_sharpe_of_returns


def test_family_trials_set_the_deflation(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    cfg = config(tmp_path)
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    registry.add_hypothesis_version(engine, "H-0001", title="t", family_id="rules", yaml_text="x\n")
    returns = np.random.default_rng(3).normal(0.0005, 0.01, 500)
    with pytest.raises(ValueError, match="no trials"):
        deflated_sharpe_for_family(cfg, engine, "rules", returns, periods_per_year=252)
    with experiment_run(
        cfg, engine, "H-0001", {}, kind="baseline", seed=1, exploratory=True
    ) as run:
        for i, sharpe in enumerate([0.5, 1.0, 1.5]):  # annualized, as trials record them
            run.record_trial(
                family_id="rules", config={"i": i}, evaluated_on_test=True, sharpe=sharpe
            )
    result = deflated_sharpe_for_family(cfg, engine, "rules", returns, periods_per_year=252)
    assert result.n_trials == 3  # trials without returns count as independent
    assert result.sharpe_variance == pytest.approx(0.25 / 252)
    expected = deflated_sharpe_of_returns(returns, n_trials=3, sharpe_variance=0.25 / 252)
    assert result.dsr == pytest.approx(expected.dsr)
    engine.dispose()
