"""BASE-005: the board configuration is validated and fixed; the report marks every net figure.
C-15 (ADR 0061): rule strategies run on every signal timeframe as ``<name>@<timeframe>``."""

import math
from pathlib import Path

import pandas as pd
import pytest

from xq.backtest.costs import SCREENING_LABEL
from xq.core.errors import ConfigError
from xq.models.baselines import RuleStrategyConfig
from xq.models.board import BoardConfig, BoardRule, load_board_config, render_board

BASE = {
    "targets": ["fwd_ret_mid_1h"],
    "walk_forward": {"min_train": "30D", "test_len": "10D"},
    "forecast_baselines": ["zero_return", "historical_mean"],
    "signal_timeframes": ["1d"],
    "rules": {
        "hold": {"rule": "buy_and_hold"},
        "hold_vol_only": {"rule": "buy_and_hold", "vol_target": True},
    },
    "vol_target": {"annual_vol": 0.1, "lookback": 20, "max_exposure": 2.0},
}


def test_every_rule_also_runs_volatility_targeted() -> None:
    board = BoardConfig.model_validate(BASE)
    assert list(board.strategies()) == ["hold@1d", "hold_vol_only@1d", "hold_vol@1d"]
    assert board.strategies()["hold_vol@1d"].rule.vol_target
    without = BoardConfig.model_validate({**BASE, "vol_target": None})
    assert list(without.strategies()) == ["hold@1d", "hold_vol_only@1d"]


def test_every_rule_runs_on_every_signal_timeframe() -> None:
    board = BoardConfig.model_validate({**BASE, "signal_timeframes": ["1d", "1h"]})
    strategies = board.strategies()
    assert list(strategies) == [
        f"{name}@{tf}" for tf in ("1d", "1h") for name in ("hold", "hold_vol_only", "hold_vol")
    ]
    assert strategies["hold_vol@1h"] == BoardRule(
        RuleStrategyConfig(rule="buy_and_hold", vol_target=True), "1h"
    )
    assert strategies["hold@1d"].timeframe == "1d"


def test_signal_timeframes_are_validated() -> None:
    with pytest.raises(ValueError, match="signal_timeframe"):  # the legacy key is gone
        BoardConfig.model_validate({**BASE, "signal_timeframe": "1d"})
    no_timeframe = {k: v for k, v in BASE.items() if k != "signal_timeframes"}
    with pytest.raises(ConfigError, match="at least one signal timeframe"):
        BoardConfig.model_validate(no_timeframe)
    assert BoardConfig.model_validate({**no_timeframe, "rules": {}}).strategies() == {}
    with pytest.raises(ConfigError, match="repeat"):
        BoardConfig.model_validate({**BASE, "signal_timeframes": ["1d", "1d"]})
    with pytest.raises(ConfigError, match="unknown signal timeframe"):
        BoardConfig.model_validate({**BASE, "signal_timeframes": ["2d"]})
    with pytest.raises(ConfigError, match="must not contain"):
        BoardConfig.model_validate({**BASE, "rules": {"a@b": {"rule": "buy_and_hold"}}})


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
        "signal_timeframes": ["1d", "1h"],
        "targets": ["fwd_ret_mid_1h"],
        "history_start": "h0",
        "history_end": "h1",
        "history_days": 10,
        "oos_start": "a",
        "oos_end": "b",
        "oos_days": 3,
        "folds": 1,
        "periods_per_year": 252,
        "bootstrap": {"n_boot": 10000, "block_length": "politis_white", "min_block_days": 5},
        "trials": {"raw": 30, "effective": 2, "gated": "effective", "review": True},
        "slices": {
            "label": "descriptive (not tested: no p-values, no trials)",
            "declared": ["year", "session"],
            "hypothesis": "H-0001 v1",
            "error": None,
            "strategies": {
                "hold": {
                    "year": {
                        "label": "descriptive",
                        "rows": [
                            {
                                "bucket": "2024",
                                "days": 3,
                                "net_pnl": 5.0,
                                "pnl_share": 1.0,
                                "sharpe": 0.4,
                                "positive_days": 0.5,
                            }
                        ],
                    },
                    "session": {"label": "descriptive", "rows": []},
                },
                "other": {"error": "no sliced trading day has a sigma-hat"},
            },
        },
    }
    row = {
        "strategy": "hold",
        "target": None,
        "signal_timeframe": "1d",
        "warmup_bars": 1,
        "pre_start_bars": 0,
        "period": "full_history",
        "evaluation_start": "2024-01-02 00:00:00+00:00",
        "evaluation_days": 3,
        "fold_sharpe": 0.3,
        "fold_annual_return": 0.02,
        "fold_net_pnl": 4.0,
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
    assert len(rows) == 5  # the board, the fold-aligned view and one year slice
    assert all(line.endswith(f"| {SCREENING_LABEL} |") for line in rows)
    assert "rule signals on 1d, 1h bars" in text
    assert "| hold | — | 1d | 1 (0) | full_history | 2024-01-02 00:00:00+00:00 | 3 |" in text
    assert "## Fold-aligned view — descriptive comparison, no p-values" in text
    assert "### By year — descriptive" in text
    assert "### By session — descriptive" in text
    assert "other: slices not computed (no sliced trading day has a sigma-hat)" in text
    assert f"**Every net figure below is {SCREENING_LABEL}.**" in text
    assert "owner review" in text  # 30 raw trials over 2 effective exceeds the review ratio
    assert "n/a" in rows[0]  # an undefined DSR is shown, not hidden
