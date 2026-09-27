"""The `VolForecaster` interface (VOL-006) and the platform's sigma-hat selection.

A volatility forecaster works on a **periods frame**: one row per hour or trading day from
`xq.research.volatility.realized.realized_measures` (``ret``, ``rv``, ``bv`` ...), indexed by
each period's tz-aware decision time, in time order. The plan's interface:

- ``fit(periods_train) -> Self`` — learns from the training periods only;
- ``predict_variance(periods_upto_t, horizon)`` — at every row t, the forecast made at t's
  decision time of the variance of the next `horizon` periods' summed return, i.e. of
  ``rv_{t+1} + ... + rv_{t+horizon}``; it uses rows up to t only (a test perturbs later rows);
- ``predict(periods_upto_t, horizon)`` — sigma-hat, its square root, indexed by decision time.

Forecasters never size positions (CLAUDE.md): sigma-hat scales targets, stops and costs; sizing
is the risk engine's.

**Selection** (`select_forecaster`, the plan's promotion rule): a model is selected only if it is in
the 90 % Model Confidence Set **and** beats the default (``ewma_0.94``) by a one-sided
Diebold-Mariano test on QLIKE whose p-value, **Holm-adjusted across all challengers** (every model
other than the default with a defined p-value), is below ``dm_alpha``; among such models the lowest
mean QLIKE wins. Without the adjustment, a board of twelve challengers that are no better than the
default promotes one of them in about 30 % of simulated samples (up to 46 % for twelve independent
tests); with it, in at most about ``dm_alpha`` of them (a test simulates this). When nothing beats
the default — including when nothing else was evaluated — the default stays. The selection is a
record, not a switch: nothing here writes it anywhere. Replacing the platform's sigma-hat (the
interim EWMA of ``fwd_returns.v1``) needs the owner's approval, an ADR and a configuration change
after a board on real data (Sprint 6 is build-only; no model is promoted, ADR 0044).

**Serving** (`serve_sigma`): the selected forecaster serves sigma-hat per walk-forward fold — fitted
on each fold's training periods only, forecasting its test periods — so every sigma-hat a later
stage reads was produced without the data it is used on.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

import numpy as np
import pandas as pd

from xq.core.errors import NaiveTimestampError
from xq.research.stats.results import holm_adjust
from xq.validation.splitters import WalkForwardConfig, WalkForwardSplitter

#: Columns every periods frame carries (`xq.research.volatility.realized.REALIZED_COLUMNS`).
PERIOD_COLUMNS = ("period_start", "ret", "rv")


class VolForecaster(ABC):
    """Base class of every volatility forecaster (module docstring)."""

    #: The forecaster's name on the volatility board.
    name: str = "forecaster"

    def fit(self, periods: pd.DataFrame) -> Self:
        """Fit on training periods only; return self."""
        check_periods(periods)
        if periods.empty:
            raise ValueError(f"{self.name}: no training periods")
        self._fit(periods)
        return self

    def predict_variance(self, periods: pd.DataFrame, horizon: int) -> pd.Series:
        """The variance forecast of the next `horizon` periods at every row (module docstring)."""
        check_periods(periods)
        if horizon < 1:
            raise ValueError("horizon must be at least 1 period")
        values = np.asarray(self._predict(periods, horizon), dtype=np.float64)
        if values.shape != (len(periods),):
            raise ValueError(f"{self.name} returned {values.shape} forecasts for {len(periods)}")
        return pd.Series(values, index=periods.index, name=self.name)

    def predict(self, periods: pd.DataFrame, horizon: int) -> pd.Series:
        """Sigma-hat of the next `horizon` periods' return at every row."""
        variance = self.predict_variance(periods, horizon).to_numpy(np.float64)
        return pd.Series(np.sqrt(np.maximum(variance, 0.0)), index=periods.index, name=self.name)

    @abstractmethod
    def _fit(self, periods: pd.DataFrame) -> None:
        """Learn from the training periods."""

    @abstractmethod
    def _predict(self, periods: pd.DataFrame, horizon: int) -> np.ndarray:
        """One causal variance forecast per row of `periods`."""


def check_periods(periods: pd.DataFrame) -> None:
    """Raise unless `periods` is indexed by increasing tz-aware decision times with the columns.

    Raises:
        NaiveTimestampError: for a naive index.
        ValueError: for an unsorted index or missing columns.
    """
    index = periods.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise NaiveTimestampError("periods must be indexed by tz-aware decision times")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("periods must be in strictly increasing decision-time order")
    missing = [c for c in PERIOD_COLUMNS if c not in periods.columns]
    if missing:
        raise ValueError(f"periods lack columns {missing}")


def realized_target(periods: pd.DataFrame, horizon: int) -> tuple[pd.Series, pd.Series]:
    """``rv_{t+1} + ... + rv_{t+horizon}`` and its ``label_end`` per row (missing at the end)."""
    rv = periods["rv"].to_numpy(np.float64)
    n = len(rv)
    cumulative = np.r_[0.0, np.cumsum(rv)]
    y = np.full(n, np.nan)
    ends = pd.Series(pd.NaT, index=periods.index, dtype="datetime64[ns, UTC]")
    if n > horizon:
        y[: n - horizon] = cumulative[horizon + 1 :] - cumulative[1 : n - horizon + 1]
        ends.iloc[: n - horizon] = periods.index[horizon:]
    return pd.Series(y, index=periods.index, name="target"), ends


@dataclass(frozen=True)
class Selection:
    """Which forecaster serves sigma-hat, and why every candidate was or was not selected."""

    selected: str
    default: str
    beats_default: bool
    eligible: tuple[str, ...]
    reasons: dict[str, str]
    #: Each challenger's one-sided DM p-value against the default, Holm-adjusted across them.
    p_holm: dict[str, float] = field(default_factory=dict)


def select_forecaster(metrics: pd.DataFrame, *, default: str, dm_alpha: float) -> Selection:
    """Apply the selection rule (module docstring) to a volatility board's metrics.

    Args:
        metrics: One row per model with ``model``, ``qlike``, ``in_mcs`` and
            ``dm_vs_default_p_less`` (`xq.research.volatility.evaluate.VolBoard.metrics`), the
            unadjusted one-sided p-value of each model beating the default.

    Raises:
        ValueError: if the default is not on the board.
    """
    table = metrics.set_index("model")
    if default not in table.index:
        raise ValueError(f"the default {default!r} is not on the board")
    challengers = [str(name) for name in table.index if str(name) != default]
    raw = table.loc[challengers, "dm_vs_default_p_less"].to_numpy(dtype=np.float64)
    adjusted = dict(zip(challengers, (float(p) for p in holm_adjust(raw)), strict=True))
    reasons: dict[str, str] = {default: "the default: kept unless a model beats it"}
    eligible: list[str] = []
    for model in challengers:
        p = adjusted[model]
        if not bool(table.loc[model, "in_mcs"]):
            reasons[model] = "not in the model confidence set"
        elif not (math.isfinite(p) and p < dm_alpha):
            reasons[model] = (
                f"does not beat {default} (one-sided DM p {p:.3g} after Holm across "
                f"{len(challengers)} challengers >= {dm_alpha:g})"
            )
        else:
            eligible.append(model)
            reasons[model] = (
                f"in the MCS and beats {default} (one-sided DM p {p:.3g} after Holm across "
                f"{len(challengers)} challengers)"
            )
    if not eligible:
        return Selection(default, default, False, (), reasons, adjusted)
    qlike = table["qlike"].astype(float)
    winner = min(eligible, key=lambda m: float(qlike[m]))
    reasons[winner] += "; lowest mean QLIKE among eligible models: selected"
    return Selection(winner, default, True, tuple(eligible), reasons, adjusted)


def serve_sigma(
    periods: pd.DataFrame,
    factory: Callable[[], VolForecaster],
    horizon: int,
    splitter: WalkForwardConfig,
) -> pd.DataFrame:
    """Sigma-hat of the next `horizon` periods on every test period, fitted per fold.

    Returns:
        Indexed by decision time: ``fold_id``, ``train_end`` (the last training decision) and
        ``sigma_hat``.

    Raises:
        ValueError: if the splitter gives no fold.
    """
    check_periods(periods)
    _, label_end = realized_target(periods, horizon)
    folds = WalkForwardSplitter(splitter).split(pd.DatetimeIndex(periods.index), label_end)
    if not folds:
        raise ValueError("the walk-forward splitter gives no fold on these periods")
    frames = []
    for fold in folds:
        train = periods.iloc[fold.train_idx]
        model = factory().fit(train)
        upto = periods.iloc[: int(fold.test_idx[-1]) + 1]
        sigma = model.predict(upto, horizon).to_numpy(np.float64)[fold.test_idx]
        frames.append(
            pd.DataFrame(
                {"fold_id": fold.fold_id, "train_end": train.index[-1], "sigma_hat": sigma},
                index=periods.index[fold.test_idx],
            )
        )
    return pd.concat(frames)
