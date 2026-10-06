"""C-33 (4): gated features are computed and checked, but a model input request for one is
refused until its gate is admitted (FEAT-007 for tick weights)."""

import numpy as np
import pandas as pd
import pytest

from helpers.features import CFG
from xq.core.config import FeatureInstanceConfig, FeatureSetConfig
from xq.core.errors import ConfigError
from xq.features.base import GATES, register_feature
from xq.features.price import Sigma, session_vwap
from xq.features.registry import (
    GatedFeatureError,
    feature_specs,
    model_inputs,
    output_prefix,
)

CORE = CFG.feature_set_config("core", "v2")


def feature_frame(definition: FeatureSetConfig) -> pd.DataFrame:
    """A frame with every column the set writes (multi-column features with two sub-columns) and
    a provenance column per context timeframe."""
    columns: list[str] = []
    for spec in feature_specs(definition):
        name = output_prefix(spec) + spec.column
        columns += (
            [name] if spec.name not in ("candle", "swing", "adx") else [f"{name}_a", f"{name}_b"]
        )
    columns += ["mtf_1h_available_at", "close"]
    return pd.DataFrame(np.zeros((3, len(columns))), columns=columns)


def test_the_vwap_distance_is_the_only_gated_feature_of_core_v2() -> None:
    gated = [s for s in feature_specs(CORE) if s.gate is not None]
    assert [(s.column, s.gate) for s in gated] == [("session_vwap_96", "tick_volume")]
    assert session_vwap.gate == "tick_volume"
    assert "FEAT-007" in GATES["tick_volume"]
    assert [i.feature for i in CORE.gated] == ["session_vwap"]
    assert "session_vwap" not in [i.feature for i in CORE.features]


def test_a_model_input_request_for_a_gated_feature_is_refused() -> None:
    frame = feature_frame(CORE)
    with pytest.raises(GatedFeatureError, match="FEAT-007"):
        model_inputs(frame, CORE, ["rsi_14", "session_vwap_96"])
    # by default every ungated feature column, never the gated one, provenance or base columns
    default = list(model_inputs(frame, CORE).columns)
    assert "session_vwap_96" not in default
    assert {"rsi_14", "candle_a", "mtf_1d_rsi_14"} <= set(default)
    assert "mtf_1h_available_at" not in default
    assert "close" not in default
    assert list(model_inputs(frame, CORE, ["rsi_14"]).columns) == ["rsi_14"]
    with pytest.raises(ConfigError, match="not a feature column"):
        model_inputs(frame, CORE, ["mtf_1h_available_at"])
    with pytest.raises(ConfigError, match="not a feature column"):
        model_inputs(frame, CORE, ["nope"])


def test_gated_and_ungated_features_stay_in_their_groups() -> None:
    misplaced = FeatureSetConfig(
        features=[FeatureInstanceConfig(feature="session_vwap", params={"sigma_span": 96})]
    )
    with pytest.raises(ConfigError, match="list it under `gated`"):
        feature_specs(misplaced)
    wrong = FeatureSetConfig(
        features=[FeatureInstanceConfig(feature="rsi", params={"window": 14})],
        gated=[FeatureInstanceConfig(feature="rsi", params={"window": 14})],
    )
    with pytest.raises(ConfigError, match="has no gate"):
        feature_specs(wrong)
    with pytest.raises(ConfigError, match="unknown gate"):
        register_feature(
            name="x", version=1, family="price", params=Sigma, lookback=lambda p: 1,
            warmup=lambda p: 1, gate="magic",
        )  # fmt: skip
