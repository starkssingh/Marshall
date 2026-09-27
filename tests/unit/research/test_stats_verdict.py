"""STAT-008: verdicts in the Observed / Evidence / Interpretation / Limitations / Action format,
built from simulated results only, read the evidence the way the plan's method table requires,
cite recovery tests, and are written as a deterministic report."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.simulate import (
    ar1,
    bar_frame,
    forward_target,
    garch_returns,
    ornstein_uhlenbeck,
    random_walk,
)
from xq.core.config import (
    ArmaSpec,
    StationarityConfig,
    StatsDependenceConfig,
)
from xq.research.stats.arima import HorizonTarget, arma_study
from xq.research.stats.dependence import dependence_tests
from xq.research.stats.stationarity import stationarity_battery
from xq.research.stats.variance_ratio import variance_ratio_tests
from xq.research.stats.verdict import (
    MethodVerdict,
    arma_verdict,
    build_verdict_report,
    dependence_verdict,
    stationarity_verdict,
    variance_ratio_verdict,
)
from xq.validation.splitters import WalkForwardConfig

ALPHA = 0.05
STATIONARITY = StationarityConfig(kpss_trends=["c", "ct"], zivot_andrews_trim=0.15)
DEPENDENCE = StatsDependenceConfig(ljung_box_lags=[1, 5, 10], arch_lm_lags=[5])


def _verdicts() -> list[MethodVerdict]:
    price = stationarity_verdict(
        stationarity_battery(random_walk(1500, seed=1), "log_price", STATIONARITY, ALPHA)
    )
    garch = dependence_verdict(
        dependence_tests(garch_returns(3000, alpha=0.1, beta=0.85, seed=2), "r", DEPENDENCE, ALPHA)
    )
    ou = variance_ratio_verdict(
        variance_ratio_tests(
            np.diff(ornstein_uhlenbeck(20_000, theta=0.05, seed=3)), "ou", [2, 4, 8], ALPHA
        )
    )
    frame = bar_frame(0.001 * ar1(4000, 0.2, seed=4))
    y, ends = forward_target(frame, 1)
    study = arma_study(
        frame,
        [HorizonTarget("15m", y, ends, 1, ("open", "close"))],
        {"ar1": ArmaSpec(p=1)},
        ["zero_return", "random_walk"],
        WalkForwardConfig(min_train="15D", test_len="10D"),
        alpha=ALPHA,
        seed=1,
    )
    return [price, garch, ou, arma_verdict(study, "r_15m")]


def test_verdicts_read_simulated_evidence_as_the_plan_requires() -> None:
    price, garch, ou, arma = _verdicts()
    assert price.status == "evidence"
    assert "Model its differences" in price.action
    assert garch.status == "no evidence"  # GARCH returns are uncorrelated
    assert "clustering is not return predictability" in garch.action
    assert "Volatility clusters" in garch.interpretation
    assert ou.status == "evidence"
    assert "reversion" in ou.interpretation
    assert arma.status == "useful evidence"
    assert "recorded, not evidence" in arma.evidence


def test_iid_returns_give_no_evidence_anywhere() -> None:
    r = np.random.default_rng(7).standard_normal(3000)
    assert dependence_verdict(dependence_tests(r, "iid", DEPENDENCE, ALPHA)).status == (
        "no evidence"
    )
    vr = variance_ratio_verdict(variance_ratio_tests(r, "iid", [2, 4, 8], ALPHA))
    assert vr.status == "no evidence"


def test_every_field_and_a_recovery_test_are_required() -> None:
    price = _verdicts()[0]
    fields = {k: getattr(price, k) for k in MethodVerdict.__dataclass_fields__}
    with pytest.raises(ValueError, match="empty"):
        MethodVerdict(**{**fields, "limitations": "  "})
    with pytest.raises(ValueError, match="recovery"):
        MethodVerdict(**{**fields, "recovery": ()})
    block = price.markdown()
    for label in ("Observed", "Evidence", "Interpretation", "Limitations", "Action"):
        assert f"**{label}:**" in block
    assert "test_random_walk_adf_does_not_reject" in block


def test_the_report_is_deterministic(tmp_path: Path) -> None:
    verdicts = _verdicts()
    metadata = {"data": "simulated processes (build-only, no real data)", "git_sha": "test"}
    first = build_verdict_report(verdicts, tmp_path / "a", metadata=metadata)
    second = build_verdict_report(_verdicts(), tmp_path / "b", metadata=metadata)
    assert first == second
    text = (tmp_path / "a" / "verdict.md").read_text()
    assert text.count("**Observed:**") == 4
    assert "useful evidence" in text
    summary = pd.read_csv(tmp_path / "a" / "tables" / "verdict-summary.csv")
    assert list(summary["task"]) == ["STAT-001", "STAT-002", "STAT-003", "STAT-006"]
    manifest = json.loads((tmp_path / "a" / "manifest.json").read_text())
    assert "verdict.md" in manifest["files"]


def test_an_empty_report_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one verdict"):
        build_verdict_report([], tmp_path, metadata={})
