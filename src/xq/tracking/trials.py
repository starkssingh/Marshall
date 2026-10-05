"""Trial counter and effective number of independent trials (EXP-004).

A *trial* is one evaluated configuration. Every configuration a run evaluates is recorded with
`record_trial` — including the ones that looked bad — because multiple-testing corrections (the
deflated Sharpe ratio, SPA) are only as honest as the count they are given. Trials that were
evaluated on test folds are flagged; walk-forward runs (Sprint 4) record one trial per evaluated
configuration.

`trial_count` reports, per family or globally:

- ``n_trials`` and ``n_test_evaluations``;
- ``effective_n``, the effective number of independent trials: trials whose return series are
  highly correlated **in absolute value** are near-duplicates or mirror images of each other, so
  they are clustered (average linkage on ``1 - |rho|``, cut at ``1 - correlation_threshold``) and
  each cluster counts once. A rule and its mirror (the same signal traded the other way) are one
  choice, not two independent ones (C-25, ADR 0058). Returns are first summed per trading day
  (17:00 New York roll), so trials sampled at different frequencies compare on the same footing
  and intraday noise does not dilute the correlation (ADR 0026). Trials without returns, and pairs
  with fewer than ``min_common_days`` common trading days, count as independent — the
  conservative direction, since more trials raise the bar a candidate must clear;
- ``sharpe_variance``, the variance of the trials' Sharpe ratios **across clusters** (an input to
  the DSR, whose benchmark is the expected maximum of ``effective_n`` independent trials with
  that variance). Each cluster contributes one value: the mean recorded Sharpe ratio of its
  members that trade in the same direction as its anchor, the first of its trials in recording
  order that has a Sharpe ratio. A member trades in the anchor's direction when its returns
  correlate positively with the anchor's. A mirror image therefore adds nothing, and its Sharpe
  ratio is not negated: a negated net return would count its costs as income. Taken over raw
  trials, the variance mixed a raw count with a cluster count and a mirror inflated it (``s`` and
  about ``-s``), which broke the DSR's benchmark (ADR 0056). A trial without returns is its own
  cluster, with its own Sharpe ratio.

In ML research a trial is a distinct pipeline specification evaluated on outer test folds, not
an inner search configuration (C-34 (4), `xq.models.hpo.record_pipeline_trial`, which finds an
existing trial of the same specification with `find_family_trial`).

Forecasting models evaluated on test folds are trials too, but of their own families
(`LINEAR_FORECAST_FAMILY` for STAT-006, `VOLATILITY_MODEL_FAMILY` for VOL-005, both in
`xq.tracking.registry`), never of a trading-strategy family such as ``baselines``: a strategy's
deflated Sharpe ratio counts the strategies of its family, not the forecasting models studied
beside it (ADR 0046). Those family ids are reserved: no hypothesis can be registered in one
(`xq.tracking.registry.RESERVED_FAMILIES`, ADR 0047).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
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

    In a reproduction (`RunContext.reproduces`), a configuration the original run already
    recorded in the same family is not recorded again; its original trial id is returned.

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
    if run.reproduces is not None:
        # a reproduction re-evaluates the original run's configurations on the same data: they
        # are already counted, so counting them again would inflate the family's trials (EXP-006)
        existing = find_trial(run.engine, run.reproduces, family_id, trial_config_hash(config))
        if existing is not None:
            return existing
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


def find_trial(engine: Engine, run_id: str, family_id: str, config_hash: str) -> str | None:
    """The id of a trial of `run_id` with this family and configuration hash, if one exists."""
    with session_factory(engine)() as session:
        query = select(Trial.trial_id).where(
            Trial.run_id == run_id, Trial.family_id == family_id, Trial.config_hash == config_hash
        )
        return session.scalars(query.order_by(Trial.created_at)).first()


def find_family_trial(engine: Engine, family_id: str, config_hash: str) -> str | None:
    """The id of the first trial of `family_id` with this configuration hash, in any run."""
    with session_factory(engine)() as session:
        query = select(Trial.trial_id).where(
            Trial.family_id == family_id, Trial.config_hash == config_hash
        )
        return session.scalars(query.order_by(Trial.created_at, Trial.trial_id)).first()


def trial_count(cfg: AppConfig, engine: Engine, family_id: str | None = None) -> TrialStats:
    """Trial statistics for `family_id`, or across all families when None."""
    with session_factory(engine)() as session:
        query = select(Trial).order_by(Trial.created_at, Trial.trial_id)
        if family_id is not None:
            query = query.where(Trial.family_id == family_id)
        trials = session.scalars(query).all()
        rows = [(t.evaluated_on_test, t.sharpe, t.returns_path) for t in trials]
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    clusters = cluster_trials(
        [_read_returns(data_dir / path) if path else None for _, _, path in rows],
        cfg.experiments_config().trial_clustering,
    )
    return TrialStats(
        family_id=family_id,
        n_trials=len(rows),
        n_test_evaluations=sum(1 for on_test, _, _ in rows if on_test),
        effective_n=clusters.n_clusters,
        sharpe_variance=clusters.sharpe_variance([s for _, s, _ in rows]),
    )


@dataclass(frozen=True)
class TrialClusters:
    """Clusters of a family's trials (module docstring), one entry per trial in recording order."""

    #: Cluster label of each trial.
    labels: tuple[int, ...]
    #: +1 or -1: the sign of each trial's correlation with its cluster's first trial with returns;
    #: two members of a cluster trade in the same direction when their signs are equal.
    signs: tuple[float, ...]

    @property
    def n_clusters(self) -> int:
        """The effective number of independent trials."""
        return len(set(self.labels))

    def sharpe_variance(self, sharpes: Sequence[float | None]) -> float | None:
        """Variance (ddof 1) across clusters of each cluster's mean Sharpe ratio in its anchor's
        direction (module docstring); None with fewer than two clusters that have one.

        Raises:
            ValueError: if `sharpes` does not give one value (or None) per trial.
        """
        if len(sharpes) != len(self.labels):
            raise ValueError("one Sharpe ratio (or None) per trial is needed")
        direction: dict[int, float] = {}
        members: dict[int, list[float]] = {}
        for label, sign, sharpe in zip(self.labels, self.signs, sharpes, strict=True):
            if sharpe is None or not math.isfinite(sharpe):
                continue
            if direction.setdefault(label, sign) == sign:  # the anchor sets the direction
                members.setdefault(label, []).append(sharpe)
        values = np.array([np.mean(v) for v in members.values()], dtype=np.float64)
        return float(np.var(values, ddof=1)) if len(values) > 1 else None


def cluster_trials(
    returns: Sequence[pd.Series | None], params: TrialClusteringConfig
) -> TrialClusters:
    """Cluster trials by the absolute correlation of their daily returns (module docstring).

    A trial without returns (None) is its own cluster, as is a trial without
    ``min_common_days`` common trading days with any other.
    """
    labels = [0] * len(returns)
    signs = [1.0] * len(returns)
    known = [i for i, r in enumerate(returns) if r is not None]
    next_label = 1
    if len(known) > 1:
        frame = pd.concat(
            {i: daily_returns(r) for i, r in enumerate(returns) if r is not None}, axis=1
        )
        corr = frame.corr(min_periods=params.min_common_days).to_numpy(dtype=np.float64)
        corr = np.nan_to_num(corr, nan=0.0)  # too little overlap: treat as independent
        np.fill_diagonal(corr, 1.0)
        distance = np.clip(1.0 - np.abs(corr), 0.0, 1.0)
        distance = (distance + distance.T) / 2
        tree = linkage(squareform(distance, checks=False), method="average")
        found = fcluster(tree, t=1.0 - params.correlation_threshold, criterion="distance")
        anchors: dict[int, int] = {}
        for position, (i, label) in enumerate(zip(known, found, strict=True)):
            anchor = anchors.setdefault(int(label), position)
            labels[i] = int(label)
            signs[i] = -1.0 if corr[position, anchor] < 0 else 1.0
        next_label = int(found.max()) + 1
    elif known:
        labels[known[0]] = next_label
        next_label += 1
    for i, r in enumerate(returns):
        if r is None:
            labels[i] = next_label
            next_label += 1
    return TrialClusters(tuple(labels), tuple(signs))


def effective_trials(returns: Mapping[str, pd.Series], params: TrialClusteringConfig) -> int:
    """Number of clusters of return series (see the module docstring)."""
    return cluster_trials(list(returns.values()), params).n_clusters


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
