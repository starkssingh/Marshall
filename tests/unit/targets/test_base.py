"""TGT-001: target specs, long target frames and the schema guard."""

import pandas as pd
import pytest

from helpers.targets import STUB_DEFINITION as DEFINITION
from helpers.targets import STUB_KIND as KIND
from helpers.targets import stub_compute
from xq.core.config import TargetSetConfig
from xq.targets.base import (
    TargetKind,
    TargetLeakError,
    TargetSpec,
    check_feature_matrix,
    compute_targets,
    definition_hash,
    target_values,
)

T = pd.date_range("2024-03-12 10:00", periods=4, freq="15min", tz="UTC", name="decision_time")


def test_long_frame_has_one_row_per_decision_and_target() -> None:
    specs = KIND.expand(DEFINITION)
    frame = compute_targets(KIND, specs, pd.DataFrame(), pd.Series(1.0, index=T))
    assert frame.index.name == "decision_time"
    assert list(frame.columns) == ["target", "value", "label_start", "label_end", "scale"]
    assert len(frame) == len(T) * 4
    assert frame["target"].iloc[:4].tolist() == sorted(s.name for s in specs)
    one = target_values(frame, "stub_long_1h")
    assert one.index.equals(T)
    assert (one["label_end"] - one["label_start"] == pd.Timedelta("1h")).all()
    with pytest.raises(KeyError, match="available"):
        target_values(frame, "missing")


def test_kinds_must_return_every_value_column() -> None:
    def bad(spec: TargetSpec, quotes: pd.DataFrame, sigma: pd.Series) -> pd.DataFrame:
        return stub_compute(spec, quotes, sigma).drop(columns="label_end")

    broken = TargetKind("bad", 1, KIND.expand, KIND.sigma, bad, KIND.lookahead)
    with pytest.raises(ValueError, match="label_end"):
        compute_targets(broken, KIND.expand(DEFINITION), pd.DataFrame(), pd.Series(1.0, index=T))


@pytest.mark.parametrize("column", ["stub_long_1h", "label_end", "value", "tgt_anything", "fwd_x"])
def test_schema_guard_rejects_target_columns(column: str) -> None:
    features = pd.DataFrame({"close": [1.0], column: [2.0]})
    with pytest.raises(TargetLeakError, match=column):
        check_feature_matrix(features, ["stub_long_1h"])


def test_schema_guard_accepts_ordinary_features() -> None:
    check_feature_matrix(pd.DataFrame({"close": [1.0], "ctx_1h_close": [1.0]}), ["stub_long_1h"])


def test_definition_hash_and_validation() -> None:
    same = TargetSetConfig(kind="stub", horizons=["15m", "1h"], price_refs=["long", "mid"])
    assert definition_hash(same) == definition_hash(DEFINITION)
    other = TargetSetConfig(kind="stub", horizons=["15m"], price_refs=["long", "mid"])
    assert definition_hash(other) != definition_hash(DEFINITION)
    with pytest.raises(ValueError, match="positive"):
        TargetSetConfig(kind="stub", horizons=["0s"], price_refs=["long"])
    with pytest.raises(ValueError, match="unique"):
        TargetSetConfig(kind="stub", horizons=["1h", "1h"], price_refs=["long"])
    with pytest.raises(ValueError, match="invalid horizon"):
        TargetSetConfig(kind="stub", horizons=["soon"], price_refs=["long"])
