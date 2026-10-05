"""C-34 (5) on the command line: `xq model check` and `xq model predict` refuse a model written
with other library versions unless `--allow-library-drift` is given, which loads it as a
diagnostic whose outputs are labelled "diagnostic, library drift" and are not evidence."""

import json
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from helpers.pipeline import REPO
from xq.cli.main import app
from xq.core.config import load_config
from xq.core.seeds import make_rng
from xq.models.persistence import (
    CARD,
    DIAGNOSTIC_LABEL,
    NotEvidenceError,
    require_evidence,
    save_trained_fold,
)
from xq.models.pipeline import FoldData, train_fold
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter

runner = CliRunner()
PIPELINE = load_config("research", config_dir=REPO / "config").ml_config().pipeline


def stored(directory: Path) -> pd.DataFrame:
    """Store a logistic fold model in `directory`; return its test inputs."""
    rng = make_rng(5)
    times = pd.date_range("2024-01-01", periods=1500, freq="h", tz="UTC")
    x = pd.DataFrame(rng.normal(size=(1500, 2)), index=times, columns=["a", "b"])
    y = pd.Series(((x["a"] + rng.normal(0, 1, 1500)) > 0).astype(float), index=times)
    data = FoldData(x, y, pd.Series(times + pd.Timedelta(hours=3), index=times))
    fold = WalkForwardSplitter(
        WalkForwardConfig.model_validate({"min_train": "30D", "test_len": "10D", "embargo": "1D"})
    ).split(pd.DatetimeIndex(times), data.label_end)[0]
    trained = train_fold(
        "logistic", fold, data, PIPELINE, embargo=pd.Timedelta(days=1), seed=2, params={}
    )
    assert trained is not None
    save_trained_fold(directory, trained, data, dataset_id="ds-synthetic", feature_set="core.v1")
    return x.loc[trained.predictions.index]


def drift(directory: Path) -> None:
    card = json.loads((directory / CARD).read_text())
    card["library_versions"]["scikit-learn"] = "0.0.1"
    (directory / CARD).write_text(json.dumps(card))


def test_a_matching_model_is_evidence(tmp_path: Path) -> None:
    inputs = stored(tmp_path / "model")
    inputs.to_parquet(tmp_path / "inputs.parquet")
    result = runner.invoke(app, ["model", "check", str(tmp_path / "model")])
    assert result.exit_code == 0, result.output
    assert "status: evidence" in result.output
    result = runner.invoke(
        app,
        ["model", "predict", str(tmp_path / "model"), str(tmp_path / "inputs.parquet"),
         str(tmp_path / "out.parquet")],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    out = pd.read_parquet(tmp_path / "out.parquet")
    assert "label" not in out.columns
    require_evidence(out)


def test_library_drift_needs_the_override_and_yields_a_labelled_diagnostic(
    tmp_path: Path,
) -> None:
    inputs = stored(tmp_path / "model")
    inputs.to_parquet(tmp_path / "inputs.parquet")
    drift(tmp_path / "model")
    refused = runner.invoke(app, ["model", "check", str(tmp_path / "model")])
    assert refused.exit_code == 2
    assert "other library versions" in refused.output
    assert "--allow-library-drift" in refused.output
    checked = runner.invoke(
        app, ["model", "check", str(tmp_path / "model"), "--allow-library-drift"]
    )
    assert checked.exit_code == 0, checked.output
    assert f"status: {DIAGNOSTIC_LABEL}" in checked.output
    assert "scikit-learn 0.0.1 -> " in checked.output
    assert "not evidence" in checked.output
    args = ["model", "predict", str(tmp_path / "model"), str(tmp_path / "inputs.parquet"),
            str(tmp_path / "out.parquet")]  # fmt: skip
    assert runner.invoke(app, args).exit_code == 2
    assert not (tmp_path / "out.parquet").exists()
    predicted = runner.invoke(app, [*args, "--allow-library-drift"])
    assert predicted.exit_code == 0, predicted.output
    assert DIAGNOSTIC_LABEL in predicted.output
    out = pd.read_parquet(tmp_path / "out.parquet")
    assert (out["label"] == DIAGNOSTIC_LABEL).all()
    with pytest.raises(NotEvidenceError):
        require_evidence(out)
