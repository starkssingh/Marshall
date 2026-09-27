"""BASE-005: the board configuration is validated and fixed; the report marks every net figure."""

import math
from pathlib import Path

import pandas as pd
import pytest

from xq.backtest.costs import SCREENING_LABEL
from xq.core.errors import ConfigError
from xq.models.board import BoardConfig, load_board_config, render_board

BASE = {
    "targets": ["fwd_ret_mid_1h"],
    "walk_forward": {"min_train": "30D", "test_len": "10D"},
    "forecast_baselines": ["zero_return", "historical_mean"],
    "rules": {
        "hold": {"rule": "buy_and_hold"},
        "hold_vol_only": {"rule": "buy_and_hold", "vol_target": True},
    },
    "vol_target": {"annual_vol": 0.1, "lookback": 20, "max_exposure": 2.0},
}


def test_every_rule_also_runs_volatility_targeted() -> None:
    board = BoardConfig.model_validate(BASE)
    assert list(board.strategies()) == ["hold", "hold_vol_only", "hold_vol"]
    assert board.strategies()["hold_vol"].vol_target
    without = BoardConfig.model_validate({**BASE, "vol_target": None})
    assert list(without.strategies()) == ["hold", "hold_vol_only"]


def test_board_configuration_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown forecast baseline"):
        BoardConfig.model_validate({**BASE, "forecast_baselines": ["gradient_boosting"]})
    with pytest.raises(ConfigError, match="not found"):
        load_board_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("targets: []\nwalk_forward: {min_train: 30D, test_len: 10D}\n")
    with pytest.raises(ConfigError, match="invalid board configuration"):
        load_board_config(bad)


def test_config_hash_pins_the_board() -> None:
    board = BoardConfig.model_validate(BASE)
    assert board.config_hash() == BoardConfig.model_validate(BASE).config_hash()
    changed = BoardConfig.model_validate({**BASE, "random_entry_seeds": 10})
    assert changed.config_hash() != board.config_hash()


def test_rendered_board_marks_every_net_row() -> None:
    summary = {
        "dataset_id": "ds-x",
        "run_id": "r",
        "confirmatory": False,
        "hypothesis_family": "baselines",
        "cost_model": "placeholder",
        "cost_basis": SCREENING_LABEL,
        "gates_hash": "g",
        "board_hash": "b",
        "base_timeframe": "15m",
        "signal_timeframe": "1d",
        "targets": ["fwd_ret_mid_1h"],
        "oos_start": "a",
        "oos_end": "b",
        "oos_days": 3,
        "folds": 1,
        "periods_per_year": 252,
        "bootstrap": {"n_boot": 10000, "block_length": "politis_white", "min_block_days": 5},
        "trials": {"raw": 30, "effective": 2, "gated": "effective", "review": True},
    }
    row = {
        "strategy": "hold",
        "target": None,
        "cost_basis": SCREENING_LABEL,
        "sharpe": 0.5,
        "sharpe_ci_low": -0.1,
        "sharpe_ci_high": 1.1,
        "sharpe_p": 0.2,
        "dsr": math.nan,
        "psr": 0.7,
        "annual_return": 0.05,
        "annual_return_ci_low": -0.01,
        "annual_return_ci_high": 0.1,
        "max_drawdown": 0.02,
        "max_drawdown_ci_low": 0.01,
        "max_drawdown_ci_high": 0.05,
        "trade_count": 0.0,
        "random_entry_p": math.nan,
        "spread_cost": 1.0,
        "slippage_cost": 1.0,
        "commission": 1.0,
        "financing": 1.0,
    }
    text = render_board(summary, pd.DataFrame([row, {**row, "strategy": "other"}]), pd.DataFrame())
    rows = [line for line in text.splitlines() if line.startswith(("| hold", "| other"))]
    assert len(rows) == 2
    assert all(line.endswith(f"| {SCREENING_LABEL} |") for line in rows)
    assert f"**Every net figure below is {SCREENING_LABEL}.**" in text
    assert "owner review" in text  # 30 raw trials over 2 effective exceeds the review ratio
    assert "n/a" in rows[0]  # an undefined DSR is shown, not hidden
