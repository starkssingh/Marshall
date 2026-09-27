"""VAL-007: the evidence policy in config/gates.yaml loads, validates and cannot be overridden."""

import math
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from xq.core.config import GateCriterion, gates_hash, load_config
from xq.core.errors import ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"

#: The thresholds the owner approved on 2026-09-27 (ADR 0032). Changing any of them needs the
#: owner's approval and a new ADR, never a failing candidate.
APPROVED = {
    "version": 1,
    "conventions": {
        "returns": "daily_net",
        "annualization": "backtest.periods_per_year",
        "trial_count": "effective",
        "report_raw_trial_count": True,
        "raw_vs_effective_review_ratio": 10.0,
        "one_sided": True,
        "bootstrap": {"n_boot": 10000, "block_length": "politis_white", "min_block_days": 5},
    },
    "r1_research_candidate": {
        "oos_net_sharpe_min": 0.0,
        "oos_sharpe_p_max": 0.05,
        "best_baseline_p_max": 0.10,
        "best_baseline_margin_sharpe": 0.0,
        "min_oos_trades": 100,
    },
    "r2_validated": {
        "dsr_min": 0.95,
        "pbo_max": 0.20,
        "spa_p_max": 0.10,
        "stressed_costs": {
            "spread_multiplier": 1.5,
            "slippage_multiplier": 2.0,
            "net_sharpe_min": 0.0,
        },
        "parameter_neighbourhood": {"perturbation": 0.20, "profitable_share_min": 0.70},
        "positive_folds_share_min": 0.60,
        "max_single_year_pnl_share": 0.50,
        "monte_carlo_drawdown": {"quantile": 0.95, "below": 0.15},
        "oos_max_drawdown_max": 0.15,
        "decay_trend": {"significance": 0.05},
        "execution_delay": {"bars": 1, "net_sharpe_min": 0.0},
        "min_track_record": {"confidence": 0.95},
    },
    "r3_vault_pass": {
        "net_sharpe_min": 0.0,
        "walk_forward_interval": 0.90,
        "risk_limit_breaches_max": 0,
        "vault_access_logged": True,
    },
    "r4_paper_pass": {
        "min_months": 3,
        "min_trades": 100,
        "realized_slippage_ratio_max": 1.5,
        "monte_carlo_percentile_min": 0.10,
        "shadow_parity_min": 1.0,
        "unresolved_incidents_max": 0,
    },
}

#: Leaves that parameterize a criterion's measure (or the conventions) rather than being compared.
PARAMETERS = {
    "stressed_costs.spread_multiplier",
    "stressed_costs.slippage_multiplier",
    "parameter_neighbourhood.perturbation",
    "monte_carlo_drawdown.quantile",
    "execution_delay.bars",
}
GATE_SECTIONS = {
    "R1": "r1_research_candidate",
    "R2": "r2_validated",
    "R3": "r3_vault_pass",
    "R4": "r4_paper_pass",
}


def _leaves(tree: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in tree.items():
        path = f"{prefix}{key}"
        keys |= _leaves(value, f"{path}.") if isinstance(value, dict) else {path}
    return keys


def _copy_config(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, directory)
    return directory


def _with_gates(tmp_path: Path, **changes: Any) -> Path:
    """A config directory whose gates.yaml has `changes` (dotted keys) applied."""
    directory = _copy_config(tmp_path)
    data = yaml.safe_load((directory / "gates.yaml").read_text())
    for dotted, value in changes.items():
        *parents, leaf = dotted.split(".")
        node = data
        for part in parents:
            node = node[part]
        node[leaf] = value
    (directory / "gates.yaml").write_text(yaml.safe_dump(data))
    return directory


def test_repository_gates_are_the_owner_approved_values() -> None:
    gates = load_config("research", config_dir=REPO_CONFIG).gates_config()
    assert gates.model_dump(mode="json") == APPROVED


@pytest.mark.parametrize("profile", ["dev", "research", "paper", "prod"])
def test_every_profile_loads_the_same_gates(profile: str) -> None:
    cfg = load_config(profile, config_dir=REPO_CONFIG)
    assert cfg.gates_config().model_dump(mode="json") == APPROVED
    assert cfg.gate_periods_per_year() == 252


def test_gated_trial_clustering_is_the_frozen_setting() -> None:
    # gates.yaml gates on the clustered effective N "corr 0.7, >= 60 common days, frozen".
    clustering = load_config("research", config_dir=REPO_CONFIG).experiments_config()
    assert clustering.trial_clustering.correlation_threshold == 0.7
    assert clustering.trial_clustering.min_common_days == 60


def test_every_threshold_has_exactly_one_boundary_rule() -> None:
    gates = load_config("research", config_dir=REPO_CONFIG).gates_config()
    criteria = gates.criteria()
    compared = {(c.gate, c.key.removesuffix(".low").removesuffix(".high")) for c in criteria}
    for gate, section in GATE_SECTIONS.items():
        leaves = _leaves(APPROVED[section])
        assert {key for g, key in compared if g == gate} == leaves - PARAMETERS
    assert len({(c.gate, c.key) for c in criteria}) == len(criteria)


@pytest.mark.parametrize(
    ("gate", "key", "at_boundary", "inside", "outside"),
    [
        ("R1", "oos_net_sharpe_min", False, 0.01, -0.01),  # "> 0": a zero Sharpe is no edge
        ("R1", "oos_sharpe_p_max", False, 0.049, 0.051),  # "p < 0.05"
        ("R1", "best_baseline_p_max", False, 0.099, 0.101),  # "p < 0.10"
        ("R1", "min_oos_trades", True, 101, 99),  # "at least 100"
        ("R2", "dsr_min", True, 0.96, 0.94),  # "DSR >= 0.95"
        ("R2", "pbo_max", True, 0.19, 0.21),  # "PBO <= 0.20"
        ("R2", "spa_p_max", True, 0.09, 0.11),  # "SPA p <= 0.10"
        ("R2", "max_single_year_pnl_share", True, 0.4, 0.6),  # "no single year above 50 %"
        ("R2", "monte_carlo_drawdown.below", False, 0.14, 0.16),  # "below the halt level"
        ("R2", "oos_max_drawdown_max", True, 0.14, 0.16),
        ("R2", "decay_trend.significance", True, 0.2, 0.01),  # fail if significantly negative
        ("R2", "min_track_record.confidence", True, 1.5, 0.9),  # OOS days / MinTRL >= 1
        ("R3", "walk_forward_interval.low", True, 0.5, 0.01),
        ("R3", "walk_forward_interval.high", True, 0.5, 0.99),
        ("R3", "risk_limit_breaches_max", True, 0, 1),
        ("R4", "monte_carlo_percentile_min", False, 0.2, 0.05),  # "above the 10th percentile"
        ("R4", "shadow_parity_min", True, 1.0, 0.999),  # 100 % parity
    ],
)
def test_boundary_rules(
    gate: str, key: str, at_boundary: bool, inside: float, outside: float
) -> None:
    criteria = load_config("research", config_dir=REPO_CONFIG).gates_config().criteria()
    (criterion,) = [c for c in criteria if (c.gate, c.key) == (gate, key)]
    assert criterion.passes(criterion.threshold) is at_boundary
    assert criterion.passes(inside)
    assert not criterion.passes(outside)
    assert not criterion.passes(math.nan)


def test_walk_forward_interval_is_the_central_90_percent() -> None:
    criteria = load_config("research", config_dir=REPO_CONFIG).gates_config().criteria()
    bounds = {c.key: c.threshold for c in criteria if c.key.startswith("walk_forward_interval")}
    assert bounds == {"walk_forward_interval.low": 0.05, "walk_forward_interval.high": 0.95}


def test_criterion_comparisons() -> None:
    assert GateCriterion("R", "k", "m", ">", 1.0).passes(1.5)
    assert not GateCriterion("R", "k", "m", "<", 1.0).passes(1.0)
    assert GateCriterion("R", "k", "m", "<=", 1.0).passes(1.0)
    assert not GateCriterion("R", "k", "m", ">=", 1.0).passes(math.nan)


def test_a_criterion_is_found_by_gate_and_key_and_checks_a_value() -> None:
    gates = load_config("research", config_dir=REPO_CONFIG).gates_config()
    criterion = gates.criterion("R2", "stressed_costs.net_sharpe_min")
    assert (criterion.op, criterion.threshold) == (">", 0.0)
    assert criterion.check(0.3).passed
    assert not criterion.check(0.0).passed  # a Sharpe ratio of exactly 0 is no edge
    assert "FAIL" in criterion.check(math.nan).describe()
    with pytest.raises(KeyError, match="no criterion"):
        gates.criterion("R1", "pbo_max")


def test_trial_review_flag() -> None:
    conventions = load_config("research", config_dir=REPO_CONFIG).gates_config().conventions
    assert conventions.needs_trial_review(101, 10)
    assert not conventions.needs_trial_review(100, 10)  # "raw / effective > 10"
    assert not conventions.needs_trial_review(0, 0)


def test_gates_cannot_come_from_a_profile(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    with (directory / "research.yaml").open("a") as handle:
        handle.write("gates:\n  r2_validated:\n    dsr_min: 0.5\n")
    with pytest.raises(ConfigError, match=r"only be set in config/gates.yaml.*research.yaml"):
        load_config("research", config_dir=directory)


def test_gates_cannot_come_from_base_yaml(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    with (directory / "base.yaml").open("a") as handle:
        handle.write("gates:\n  version: 1\n")
    with pytest.raises(ConfigError, match=r"not in config file base.yaml"):
        load_config("research", config_dir=directory)


def test_gates_cannot_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="not in overrides"):
        load_config("research", {"gates.r2_validated.dsr_min": 0.5}, config_dir=REPO_CONFIG)
    monkeypatch.setenv("XQ_GATES__R2_VALIDATED__DSR_MIN", "0.5")
    with pytest.raises(ConfigError, match="environment variables"):
        load_config("research", config_dir=REPO_CONFIG)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"r1_research_candidate.oos_sharpe_p_max": 1.5},
        {"r1_research_candidate.oos_net_sharpe_min": -0.5},
        {"r1_research_candidate.min_oos_trades": 0},
        {"r2_validated.stressed_costs.spread_multiplier": 0.5},
        {"r2_validated.positive_folds_share_min": 0.0},
        {"r3_vault_pass.vault_access_logged": False},
        {"conventions.one_sided": False},
        {"conventions.returns": "per_bar"},
        {"conventions.bootstrap.n_boot": 100},
        {"conventions.raw_vs_effective_review_ratio": 1.0},
        {"r4_paper_pass.surprise": 1},
    ],
)
def test_invalid_gates_are_rejected(tmp_path: Path, changes: dict[str, Any]) -> None:
    directory = _with_gates(tmp_path, **changes)
    with pytest.raises(ConfigError):
        load_config("research", config_dir=directory)


def test_annualization_reference_needs_the_backtest_section(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    base = yaml.safe_load((directory / "base.yaml").read_text())
    del base["backtest"]
    (directory / "base.yaml").write_text(yaml.safe_dump(base))
    with pytest.raises(ConfigError, match=r"backtest.periods_per_year"):
        load_config("research", config_dir=directory)


def test_fixed_annualization_and_block_length_are_allowed(tmp_path: Path) -> None:
    directory = _with_gates(
        tmp_path, **{"conventions.annualization": 260, "conventions.bootstrap.block_length": 10}
    )
    cfg = load_config("research", config_dir=directory)
    assert cfg.gate_periods_per_year() == 260
    assert cfg.gates_config().conventions.bootstrap.block_length == 10


def test_config_without_gates_file(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    (directory / "gates.yaml").unlink()
    cfg = load_config("research", config_dir=directory)
    with pytest.raises(ConfigError, match="no evidence gates"):
        cfg.gates_config()


def test_gates_hash_fingerprints_the_policy(tmp_path: Path) -> None:
    repo = load_config("research", config_dir=REPO_CONFIG).gates_config()
    assert gates_hash(repo) == gates_hash(repo.model_copy())
    changed = load_config(
        "research", config_dir=_with_gates(tmp_path, **{"r2_validated.dsr_min": 0.9})
    ).gates_config()
    assert gates_hash(changed) != gates_hash(repo)
