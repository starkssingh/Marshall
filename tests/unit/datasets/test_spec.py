"""DS-001: dataset specs and dataset identity."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import xq.datasets.spec as spec_module
from xq.core.errors import ConfigError
from xq.core.types import PriceBasis, Timeframe
from xq.datasets.spec import DatasetSpec, dataset_id, dump_spec, load_spec

BASE: dict[str, Any] = {
    "name": "ds_test",
    "source": "mt5_primary",
    "instrument": "xauusd",
    "base_timeframe": "15m",
    "start": "2024-03-11T00:00:00Z",
    "end": "2024-03-16T00:00:00Z",
    "warmup": "2D",
    "context_timeframes": ["4h", "1h"],
    "feature_set": {"name": "base", "version": "v1"},
    "target_set": {"name": "fwd_returns", "version": "v1"},
    "exclusions": [
        {"trading_day": "2024-03-14", "reason": "planted defect"},
        {"trading_day": "2024-03-12", "reason": "planted defect"},
    ],
    "bar_build": "b1-0123abcd",
    "quality_run_id": "01QRUN000000000000000000AA",
    "config_digest": "0123456789abcdef",
}
RESOLVED = {
    "bar_build": "b1-0123abcd",
    "quality_run_id": "01QRUN000000000000000000AA",
    "config_digest": "0123456789abcdef",
}


def make(**changes: Any) -> DatasetSpec:
    return DatasetSpec.model_validate({**BASE, **changes})


def test_same_spec_same_id() -> None:
    first, second = make(), make()
    assert dataset_id(first) == dataset_id(second)
    assert dataset_id(first).startswith("ds-")
    assert len(dataset_id(first)) == len("ds-") + 16


def test_id_ignores_yaml_key_order_and_formatting(tmp_path: Path) -> None:
    forward = tmp_path / "a.yaml"
    backward = tmp_path / "b.yaml"
    forward.write_text(yaml.safe_dump(BASE, sort_keys=False))
    backward.write_text(yaml.safe_dump(dict(reversed(list(BASE.items()))), default_flow_style=True))
    assert dataset_id(load_spec(forward)) == dataset_id(load_spec(backward))


def test_canonical_forms_do_not_change_the_id() -> None:
    reordered = make(
        context_timeframes=["1h", "4h"],
        exclusions=list(reversed(BASE["exclusions"])),
        start="2024-03-11T02:00:00+02:00",
        warmup="P2D",
    )
    assert dataset_id(reordered) == dataset_id(make())


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "ds_other"},
        {"base_timeframe": "5m"},
        {"price_basis": "bid"},
        {"start": "2024-03-11T00:15:00Z"},
        {"end": "2024-03-15T00:00:00Z"},
        {"warmup": "3D"},
        {"context_timeframes": ["1h"]},
        {"feature_set": {"name": "base", "version": "v2"}},
        {"target_set": None},
        {"exclusions": []},
        {"bar_build": "b1-ffffffff"},
        {"quality_run_id": "01QRUN000000000000000000AB"},
        {"config_digest": "fedcba9876543210"},
    ],
)
def test_every_field_changes_the_id(changes: dict[str, Any]) -> None:
    assert dataset_id(make(**changes)) != dataset_id(make())


def test_code_version_is_part_of_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    before = dataset_id(make())
    monkeypatch.setattr(spec_module, "DATASET_CODE_VERSION", spec_module.DATASET_CODE_VERSION + 1)
    assert dataset_id(make()) != before


def test_unresolved_spec_has_no_id() -> None:
    spec = make(bar_build=None, quality_run_id=None, config_digest=None)
    assert not spec.is_resolved
    with pytest.raises(ValueError, match="resolved spec"):
        dataset_id(spec)
    for partial in ({"bar_build": None}, {"config_digest": None}):
        assert not make(**partial).is_resolved
    resolved = spec.resolved(**RESOLVED)
    assert resolved.is_resolved
    assert dataset_id(resolved) == dataset_id(make())


def test_resolving_cannot_change_pinned_values() -> None:
    with pytest.raises(ValueError, match="bar_build"):
        make().resolved(**{**RESOLVED, "bar_build": "b1-ffffffff"})
    with pytest.raises(ValueError, match="config_digest"):
        make().resolved(**{**RESOLVED, "config_digest": "fedcba9876543210"})


def test_parsed_values() -> None:
    spec = make()
    assert spec.base_timeframe is Timeframe.M15
    assert spec.price_basis is PriceBasis.MID
    assert spec.warmup == timedelta(days=2)
    assert spec.context_timeframes == [Timeframe.H1, Timeframe.H4]
    assert [e.trading_day for e in spec.exclusions] == [date(2024, 3, 12), date(2024, 3, 14)]
    assert spec.excluded_days == {date(2024, 3, 12), date(2024, 3, 14)}
    assert str(spec.feature_set) == "base.v1"
    assert spec.vault_policy == "exclude"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"start": "2024-03-11T00:00:00"}, "timezone"),
        ({"end": "2024-03-11T00:00:00Z"}, "end must be after start"),
        ({"context_timeframes": ["15m"]}, "longer than the base timeframe"),
        ({"context_timeframes": ["1h", "1h"]}, "unique"),
        ({"external_series": ["dxy"]}, "not supported yet"),
        ({"warmup": "-1D"}, "negative"),
        ({"exclusions": [BASE["exclusions"][0]] * 2}, "only once"),
        ({"exclusions": [{"trading_day": "2024-03-12", "reason": ""}]}, "at least 1"),
        ({"vault_policy": "include"}, "exclude"),
        ({"feature_set": {"name": "Base", "version": "v1"}}, "pattern"),
        ({"feature_set": {"name": "base", "version": "1"}}, "pattern"),
        ({"unknown": 1}, "Extra inputs"),
    ],
)
def test_invalid_specs_are_rejected(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make(**changes)


def test_dump_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(dump_spec(make()))
    assert load_spec(path) == make()
    assert dataset_id(load_spec(path)) == dataset_id(make())


def test_load_spec_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_spec(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a list")
    with pytest.raises(ConfigError, match="mapping"):
        load_spec(bad)
    bad.write_text(yaml.safe_dump({**BASE, "base_timeframe": "7m"}))
    with pytest.raises(ConfigError, match="invalid dataset spec"):
        load_spec(bad)
