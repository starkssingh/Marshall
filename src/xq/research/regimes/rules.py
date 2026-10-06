"""Rule-based regimes (REG-001): volatility, trend and compression/expansion.

Each regime reads causal feature columns of ``core.v2`` (``config/regimes.yaml``) and cuts them at
**quantiles of a training fold's own rows** (`fit`), never of the full sample or of test rows. A
fitted model labels any rows with those fixed cut-offs (`filter`), row by row, so its output at t
uses only the features at t: it is causal whenever its inputs are. `fit_per_fold` refits the
cut-offs on every walk-forward fold's training rows and labels that fold's test rows, giving an
out-of-sample regime series in which no label depends on data after its fold's training cutoff.

- **Volatility** (``low``, ``mid``, ``high``): sigma-hat below the first quantile, at or above the
  second, or between.
- **Trend** (``range``, ``trend_up``, ``trend_down``): a trend when the efficiency ratio, ADX and
  the absolute slope t-statistic are all at or above their quantiles; its direction is the sign of
  the slope t-statistic; otherwise a range.
- **Compression** (``compression``, ``normal``, ``expansion``): the short/long volatility ratio and
  the band-width percentile both at or below their low quantiles, both at or above their high
  quantiles, or neither.

The output follows the plan's export contract (REG-007): ``state`` (the state's code), ``label``,
one ``p_<state>`` column per state (1 for the state held: a rule is certain) and ``regime_age``
(rows since the state last changed, 1 on the first row of a run; a row without inputs breaks the
run). A row whose inputs are missing has no state: every column is missing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Self

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import (
    CompressionRegimeConfig,
    RuleRegimesConfig,
    TrendRegimeConfig,
    VolatilityRegimeConfig,
)
from xq.core.errors import XQError
from xq.validation.splitters import Fold

FloatArray = npt.NDArray[np.float64]


class RegimeError(XQError):
    """A regime model is used before it is fitted, or fitted on too few training rows."""


@dataclass(frozen=True)
class StateDescription:
    """One state of a regime model."""

    code: int
    name: str
    description: str


class RuleRegime:
    """Common machinery: training-fold quantile cut-offs, causal labelling, the export columns."""

    #: The states, in code order (set by each rule).
    STATES: tuple[StateDescription, ...] = ()

    def __init__(self, columns: Sequence[str], min_training_rows: int) -> None:
        self.columns = tuple(columns)
        self.min_training_rows = min_training_rows
        self.cutoffs_: dict[str, float] | None = None

    def fit(self, train: pd.DataFrame) -> Self:
        """Compute the cut-offs on `train` (a training fold's rows) only.

        Raises:
            RegimeError: with fewer than ``min_training_rows`` rows where every input is known.
        """
        rows = train.loc[:, list(self.columns)].dropna()
        if len(rows) < self.min_training_rows:
            raise RegimeError(
                f"{type(self).__name__} needs {self.min_training_rows} training rows with every "
                f"input, got {len(rows)}"
            )
        self.cutoffs_ = self._cutoffs(rows)
        return self

    def states(self) -> list[StateDescription]:
        """The states this model assigns."""
        return list(self.STATES)

    def filter(self, data: pd.DataFrame) -> pd.DataFrame:
        """The state of every row of `data` with the fitted cut-offs (module docstring).

        Raises:
            RegimeError: if the model is not fitted.
        """
        if self.cutoffs_ is None:
            raise RegimeError(f"fit {type(self).__name__} on a training fold first")
        values = data.loc[:, list(self.columns)]
        known = values.notna().all(axis=1).to_numpy()
        codes = np.full(len(data), np.nan)
        if known.any():
            codes[known] = self._codes(values.loc[known], self.cutoffs_)
        return export_frame(codes, self.STATES, data.index)

    def _cutoffs(self, rows: pd.DataFrame) -> dict[str, float]:
        raise NotImplementedError

    def _codes(self, rows: pd.DataFrame, cutoffs: Mapping[str, float]) -> FloatArray:
        raise NotImplementedError


class VolatilityRegime(RuleRegime):
    """Low, mid and high volatility from training-fold quantiles of sigma-hat."""

    STATES = (
        StateDescription(0, "low", "sigma-hat below the low training quantile"),
        StateDescription(1, "mid", "sigma-hat between the training quantiles"),
        StateDescription(2, "high", "sigma-hat at or above the high training quantile"),
    )

    def __init__(self, cfg: VolatilityRegimeConfig, min_training_rows: int) -> None:
        super().__init__([cfg.column], min_training_rows)
        self.cfg = cfg

    def _cutoffs(self, rows: pd.DataFrame) -> dict[str, float]:
        low, high = (float(rows[self.cfg.column].quantile(q)) for q in self.cfg.quantiles)
        return {"low": low, "high": high}

    def _codes(self, rows: pd.DataFrame, cutoffs: Mapping[str, float]) -> FloatArray:
        sigma = rows[self.cfg.column].to_numpy(np.float64)
        codes: FloatArray = np.where(
            sigma < cutoffs["low"], 0.0, np.where(sigma >= cutoffs["high"], 2.0, 1.0)
        )
        return codes


class TrendRegime(RuleRegime):
    """Range or a directional trend from efficiency ratio, ADX and the slope t-statistic."""

    STATES = (
        StateDescription(0, "range", "not every trend measure at or above its training quantile"),
        StateDescription(1, "trend_up", "a trend with a positive slope t-statistic"),
        StateDescription(2, "trend_down", "a trend with a negative slope t-statistic"),
    )

    def __init__(self, cfg: TrendRegimeConfig, min_training_rows: int) -> None:
        super().__init__(
            [cfg.efficiency_column, cfg.adx_column, cfg.slope_column], min_training_rows
        )
        self.cfg = cfg

    def _cutoffs(self, rows: pd.DataFrame) -> dict[str, float]:
        c = self.cfg
        return {
            "efficiency": float(rows[c.efficiency_column].quantile(c.efficiency_quantile)),
            "adx": float(rows[c.adx_column].quantile(c.adx_quantile)),
            "slope_abs": float(rows[c.slope_column].abs().quantile(c.slope_abs_quantile)),
        }

    def _codes(self, rows: pd.DataFrame, cutoffs: Mapping[str, float]) -> FloatArray:
        c = self.cfg
        slope = rows[c.slope_column].to_numpy(np.float64)
        trending = (
            (rows[c.efficiency_column].to_numpy(np.float64) >= cutoffs["efficiency"])
            & (rows[c.adx_column].to_numpy(np.float64) >= cutoffs["adx"])
            & (np.abs(slope) >= cutoffs["slope_abs"])
            & (slope != 0)
        )
        codes: FloatArray = np.where(trending, np.where(slope > 0, 1.0, 2.0), 0.0)
        return codes


class CompressionRegime(RuleRegime):
    """Compression, normal or expansion from the volatility ratio and the band-width percentile."""

    STATES = (
        StateDescription(0, "compression", "ratio and band width at or below their low quantiles"),
        StateDescription(1, "normal", "neither compression nor expansion"),
        StateDescription(2, "expansion", "ratio and band width at or above their high quantiles"),
    )

    def __init__(self, cfg: CompressionRegimeConfig, min_training_rows: int) -> None:
        super().__init__([cfg.ratio_column, cfg.bandwidth_column], min_training_rows)
        self.cfg = cfg

    def _cutoffs(self, rows: pd.DataFrame) -> dict[str, float]:
        c = self.cfg
        out = {}
        for name, column in (("ratio", c.ratio_column), ("bandwidth", c.bandwidth_column)):
            out[f"{name}_low"] = float(rows[column].quantile(c.low_quantile))
            out[f"{name}_high"] = float(rows[column].quantile(c.high_quantile))
        return out

    def _codes(self, rows: pd.DataFrame, cutoffs: Mapping[str, float]) -> FloatArray:
        ratio = rows[self.cfg.ratio_column].to_numpy(np.float64)
        width = rows[self.cfg.bandwidth_column].to_numpy(np.float64)
        compressed = (ratio <= cutoffs["ratio_low"]) & (width <= cutoffs["bandwidth_low"])
        expanded = (ratio >= cutoffs["ratio_high"]) & (width >= cutoffs["bandwidth_high"])
        codes: FloatArray = np.where(compressed, 0.0, np.where(expanded, 2.0, 1.0))
        return codes


def rule_regimes(cfg: RuleRegimesConfig) -> dict[str, RuleRegime]:
    """Unfitted volatility, trend and compression regimes from the configuration."""
    n = cfg.min_training_rows
    return {
        "volatility": VolatilityRegime(cfg.volatility, n),
        "trend": TrendRegime(cfg.trend, n),
        "compression": CompressionRegime(cfg.compression, n),
    }


def fit_per_fold(model: RuleRegime, features: pd.DataFrame, folds: Sequence[Fold]) -> pd.DataFrame:
    """Out-of-sample regimes: for every fold, `model` refitted on the fold's training rows labels
    the fold's test rows. Rows outside every test window, and the test rows of a fold with too few
    training rows, have no state. Adds ``fold_id`` and the fold's cut-offs as ``cutoff_<name>``.

    Raises:
        RegimeError: if two folds' test windows share a row.
    """
    out = export_frame(np.full(len(features), np.nan), model.STATES, features.index)
    out["fold_id"] = pd.Series(pd.NA, index=features.index, dtype="object")
    filled = np.zeros(len(features), dtype=bool)
    for fold in folds:
        if filled[fold.test_idx].any():
            raise RegimeError(f"fold {fold.fold_id} predicts rows another fold predicted")
        try:
            model.fit(features.iloc[fold.train_idx])
        except RegimeError:
            continue
        assert model.cutoffs_ is not None
        labelled = model.filter(features.iloc[fold.test_idx])
        rows = out.index[fold.test_idx]
        for column in labelled.columns:
            out.loc[rows, column] = labelled[column].to_numpy()
        out.loc[rows, "fold_id"] = fold.fold_id
        for name, value in model.cutoffs_.items():
            if f"cutoff_{name}" not in out:
                out[f"cutoff_{name}"] = np.nan
            out.loc[rows, f"cutoff_{name}"] = value
        filled[fold.test_idx] = True
    # regime age restarts at each fold's first test row: an age never spans two fits
    out["regime_age"] = _ages(out["state"].to_numpy(np.float64), out["fold_id"].to_numpy())
    return out


def export_frame(
    codes: FloatArray, states: Sequence[StateDescription], index: pd.Index
) -> pd.DataFrame:
    """The REG-007 columns for state `codes` (NaN where unknown)."""
    names = np.array([s.name for s in states], dtype=object)
    known = ~np.isnan(codes)
    label = np.full(len(codes), None, dtype=object)
    label[known] = names[codes[known].astype(np.int64)]
    frame = pd.DataFrame({"state": codes, "label": label}, index=index)
    for state in states:
        frame[f"p_{state.name}"] = np.where(known, (codes == state.code).astype(np.float64), np.nan)
    frame["regime_age"] = _ages(codes, np.zeros(len(codes)))
    return frame


def _ages(codes: FloatArray, groups: npt.NDArray[np.object_] | FloatArray) -> FloatArray:
    """Rows since the state last changed (1 on a run's first row), within each group; NaN where
    the state is unknown, which also ends a run."""
    ages = np.full(len(codes), np.nan)
    run = 0
    for i in range(len(codes)):
        if np.isnan(codes[i]):
            run = 0
            continue
        same = i > 0 and run > 0 and codes[i] == codes[i - 1] and groups[i] == groups[i - 1]
        run = run + 1 if same else 1
        ages[i] = run
    return ages
