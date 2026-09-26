"""Trial counter and effective number of independent trials (EXP-004).

A *trial* is one evaluated configuration. Every configuration a run evaluates is recorded with
`record_trial` — including the ones that looked bad — because multiple-testing corrections (the
deflated Sharpe ratio, SPA) are only as honest as the count they are given. Trials that were
evaluated on test folds are flagged; walk-forward runs (Sprint 4) record one trial per evaluated
configuration.

`trial_count` reports, per family or globally:

- ``n_trials`` and ``n_test_evaluations``;
- ``effective_n``, the effective number of independent trials: trials whose return series are
  highly correlated are near-duplicates, so they are clustered (average linkage on ``1 - rho``,
  cut at ``1 - correlation_threshold``) and each cluster counts once. Returns are first summed
  per trading day (17:00 New York roll), so trials sampled at different frequencies compare on
  the same footing and intraday noise does not dilute the correlation (ADR 0026). Trials without
  returns, and pairs with fewer than ``min_common_days`` common trading days, count as
  independent — the conservative direction, since more trials raise the bar a candidate must
  clear;
- ``sharpe_variance``, the variance of the recorded trial Sharpe ratios (an input to the DSR).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform
from sqlalchemy import Engine, select

from xq.core.config import AppConfig, TrialClusteringConfig
from xq.core.errors import NaiveTimestampError
from xq.core.ids import new_ulid
from xq.core.time import trading_days, utc_now
from xq.tracking.db import session_factory
from xq.tracking.models import Run, Trial
from xq.tracking.registry import RegistryError, RunStatus

if TYPE_CHECKING:
    from xq.tracking.runs import RunContext

ARTIFACTS_DIR = "artifacts"
RETURNS_COLUMN = "ret"


@dataclass(frozen=True)
class TrialStats:
    """Trial counts of one family (or of all families when `family_id` is None)."""

    family_id: str | None
    n_trials: int
    n_test_evaluations: int
    effective_n: int
    sharpe_variance: float | None


def trial_config_hash(config: Mapping[str, Any]) -> str:
    """16-hex hash of a trial's configuration."""
    canonical = json.dumps(dict(config), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def record_trial(
    run: RunContext,
    *,
    family_id: str,
    config: Mapping[str, Any],
    evaluated_on_test: bool,
    sharpe: float | None = None,
    returns: pd.Series | None = None,
) -> str:
    """Record one evaluated configuration of a running run; return the trial id.

    Args:
        run: The running experiment run (trials outside a run context are not accepted).
        family_id: Trial family, usually the hypothesis family.
        config: The configuration evaluated.
        evaluated_on_test: True if the configuration was scored on test folds.
        sharpe: Its Sharpe ratio, if computed.
        returns: Its return series (tz-aware index), stored under
            ``data/artifacts/<experiment>/<run>/trials/<trial>.parquet`` for the effective-trial
            estimate.
    """
    trial_id = new_ulid()
    relative: str | None = None
    if returns is not None:
        if not isinstance(returns.index, pd.DatetimeIndex) or returns.index.tz is None:
            raise NaiveTimestampError("trial returns need a tz-aware DatetimeIndex")
        relative = f"{ARTIFACTS_DIR}/{run.run.experiment_id}/{run.run_id}/trials/{trial_id}.parquet"
        path = run.cfg.paths.resolve(run.cfg.paths.data_dir) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame({RETURNS_COLUMN: returns.astype(np.float64).to_numpy()})
        frame.index = pd.DatetimeIndex(returns.index.tz_convert("UTC"), name="time")
        frame.to_parquet(path)
    with session_factory(run.engine)() as session:
        record = session.get(Run, run.run_id)
        if record is None or record.status != RunStatus.RUNNING.value:
            raise RegistryError(f"run {run.run_id} is not running; trials belong to a live run")
        session.add(
            Trial(
                trial_id=trial_id,
                run_id=run.run_id,
                family_id=family_id,
                config_hash=trial_config_hash(config),
                evaluated_on_test=evaluated_on_test,
                sharpe=None if sharpe is None else float(sharpe),
                returns_path=relative,
                created_at=utc_now(),
            )
        )
        session.commit()
    return trial_id


def trial_count(cfg: AppConfig, engine: Engine, family_id: str | None = None) -> TrialStats:
    """Trial statistics for `family_id`, or across all families when None."""
    with session_factory(engine)() as session:
        query = select(Trial).order_by(Trial.created_at, Trial.trial_id)
        if family_id is not None:
            query = query.where(Trial.family_id == family_id)
        trials = session.scalars(query).all()
        rows = [(t.evaluated_on_test, t.sharpe, t.returns_path) for t in trials]
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    returns = {
        str(i): _read_returns(data_dir / path) for i, (_, _, path) in enumerate(rows) if path
    }
    without_returns = sum(1 for _, _, path in rows if not path)
    clusters = effective_trials(returns, cfg.experiments_config().trial_clustering)
    sharpes = np.array([s for _, s, _ in rows if s is not None], dtype=np.float64)
    return TrialStats(
        family_id=family_id,
        n_trials=len(rows),
        n_test_evaluations=sum(1 for on_test, _, _ in rows if on_test),
        effective_n=clusters + without_returns,
        sharpe_variance=float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else None,
    )


def effective_trials(returns: Mapping[str, pd.Series], params: TrialClusteringConfig) -> int:
    """Number of clusters of return series (see the module docstring)."""
    if len(returns) <= 1:
        return len(returns)
    frame = pd.concat({name: daily_returns(r) for name, r in returns.items()}, axis=1)
    corr = frame.corr(min_periods=params.min_common_days).to_numpy(dtype=np.float64)
    corr = np.nan_to_num(corr, nan=0.0)  # too little overlap: treat as independent
    np.fill_diagonal(corr, 1.0)
    distance = np.clip(1.0 - corr, 0.0, 2.0)
    distance = (distance + distance.T) / 2
    tree = linkage(squareform(distance, checks=False), method="average")
    labels = fcluster(tree, t=1.0 - params.correlation_threshold, criterion="distance")
    return len(np.unique(labels))


def daily_returns(returns: pd.Series) -> pd.Series:
    """A return series summed per trading day (17:00 New York roll); days without rows are absent.

    Raises:
        NaiveTimestampError: if the index is not tz-aware.
    """
    index = returns.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise NaiveTimestampError("trial returns need a tz-aware DatetimeIndex")
    days = pd.DatetimeIndex(trading_days(index), name="trading_day")
    daily: pd.Series = returns.astype(np.float64).groupby(days, sort=True).sum(min_count=1)
    return daily.dropna()


def _read_returns(path: Path) -> pd.Series:
    frame = pd.read_parquet(path)
    series: pd.Series = frame[RETURNS_COLUMN]
    return series
