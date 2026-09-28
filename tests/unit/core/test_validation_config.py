"""The validation and robustness settings (config/validation.yaml) load, and the gate's
parameter perturbation is always one of the evaluated levels (C-24, ADR 0055)."""

from pathlib import Path

import pytest

from xq.core.config import load_config
from xq.core.errors import ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"


def test_the_repository_settings_load() -> None:
    settings = load_config("research", config_dir=REPO_CONFIG).validation_config()
    assert settings.spa_size_check.warn_ratio == 1.5  # the owner's 1.5x nominal
    assert settings.perturbation.max_joint_points == 243  # 3^5: five parameters in full
    assert 0.2 in settings.perturbation.levels
    assert settings.pbo.blocks == 16
    assert settings.pbo.not_applicable_max_effective_trials == 2  # the owner's rule (C-25)


def test_pbo_blocks_must_be_even() -> None:
    with pytest.raises(ConfigError, match="even"):
        load_config("research", {"validation.pbo.blocks": 15}, config_dir=REPO_CONFIG)


def test_the_gate_perturbation_must_be_an_evaluated_level() -> None:
    with pytest.raises(ConfigError, match="gate's perturbation"):
        load_config(
            "research", {"validation.perturbation.levels": [0.1, 0.3]}, config_dir=REPO_CONFIG
        )
