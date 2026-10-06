"""ADR 0071: the exclusion list (config/exclusions.yaml) loads, enforces its rule, is file-only."""

import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from xq.core.config import ExclusionsConfig, load_config
from xq.core.errors import ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
RULE = {"check": "cal.missing_open_data", "max_missing_market_share": 0.20}


def entry(day: str = "2014-10-13", share: float = 0.435, **changes: Any) -> dict[str, Any]:
    return {
        "trading_day": day,
        "reason": "four hours still missing after the re-export",
        "missing_market_share": share,
        "quality_run_id": "01QRUN000000000000000AFTER",
        **changes,
    }


def test_the_repository_list_is_empty_under_the_approved_rule() -> None:
    cfg = load_config("research", config_dir=REPO_CONFIG)
    exclusions = cfg.exclusions
    assert exclusions is not None
    assert exclusions.rule.check == "cal.missing_open_data"
    assert exclusions.rule.max_missing_market_share == 0.20
    assert exclusions.days == {"dukascopy": []}  # filled from evidence by the local session
    assert cfg.excluded_days("dukascopy") == []
    assert cfg.excluded_days("mt5_primary") == []


def test_a_day_is_excluded_only_above_the_rule() -> None:
    listed = ExclusionsConfig.model_validate({"rule": RULE, "days": {"dukascopy": [entry()]}})
    assert listed.days["dukascopy"][0].trading_day == date(2014, 10, 13)
    for share in (0.20, 0.043):  # at the rule, and one missing hour of a 23-hour day
        with pytest.raises(ValidationError, match="does not exceed"):
            ExclusionsConfig.model_validate(
                {"rule": RULE, "days": {"dukascopy": [entry(share=share)]}}
            )


@pytest.mark.parametrize(
    "bad",
    [
        {"reason": ""},
        {"quality_run_id": ""},
        {"missing_market_share": 1.5},
        {"trading_day": "2014-13-01"},
    ],
)
def test_every_entry_needs_a_reason_and_its_evidence(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ExclusionsConfig.model_validate({"rule": RULE, "days": {"dukascopy": [entry(**bad)]}})


def test_a_day_is_listed_once() -> None:
    with pytest.raises(ValidationError, match="twice"):
        ExclusionsConfig.model_validate(
            {"rule": RULE, "days": {"dukascopy": [entry(), entry(share=0.3)]}}
        )


def test_the_rule_check_is_fixed() -> None:
    with pytest.raises(ValidationError):
        ExclusionsConfig.model_validate(
            {"rule": {**RULE, "check": "bar.missing_minutes"}, "days": {}}
        )


def copy_config(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, directory)
    return directory


def test_a_listed_source_must_exist(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    data = yaml.safe_load((directory / "exclusions.yaml").read_text())
    data["days"]["nowhere"] = [entry()]
    (directory / "exclusions.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="unknown sources"):
        load_config("research", config_dir=directory)


def test_the_list_cannot_come_from_a_profile_or_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = copy_config(tmp_path)
    with (directory / "research.yaml").open("a") as handle:
        handle.write("exclusions:\n  days:\n    dukascopy: []\n")
    with pytest.raises(ConfigError, match=r"only be set in config/exclusions.yaml"):
        load_config("research", config_dir=directory)
    with pytest.raises(ConfigError, match="not in overrides"):
        load_config(
            "research",
            {"exclusions.rule.max_missing_market_share": 0.5},
            config_dir=REPO_CONFIG,
        )
    monkeypatch.setenv("XQ_EXCLUSIONS__RULE__MAX_MISSING_MARKET_SHARE", "0.5")
    with pytest.raises(ConfigError, match="environment"):
        load_config("research", config_dir=REPO_CONFIG)
