"""Pre-registered slicing (ROB-006).

A strategy's P&L is broken down by the slices its hypothesis declared **before** any result
existed, in the ``slices`` field of ``experiments/hypotheses/H-XXXX.yaml``. The slices are read
from the registered, hash-locked text of the hypothesis version a run tested (`run_slices`,
`declared_slices`), never chosen by the caller: `DeclaredSlices` can only be built by those
loaders, so a report cannot slice by whatever looks best after the fact. Slices are descriptive:
they are reported, not tested (no p-values, no trials).

The vocabulary (names are compared in lower case, with spaces, hyphens and slashes read as
underscores):

- ``year``: calendar year of the trading day (17:00 New York roll), on daily net P&L;
- ``volatility_tercile`` (also ``volatility tercile``, ``vol_tercile``): the tercile (low, mid,
  high) of the daily sigma-hat known at the start of each trading day, cut at the 1/3 and 2/3
  quantiles of the sliced days' sigma-hat. This is an after-the-fact grouping for reporting; it
  never feeds a decision, and its table is labelled "descriptive, cut ex post" (C-24). Days
  whose sigma-hat is not known yet (the estimator's warm-up) form a ``no_sigma_hat`` bucket;
  nothing is back-filled;
- ``session``: closed trades by the session they were entered in, per ``config/sessions.yaml``
  converted to UTC per date (DST by construction): a configured overlap's name when the entry lies
  in exactly its sessions, the session's name when in one, ``+``-joined names for any other
  combination, ``off_session`` when in none;
- any name with ``regime`` (``trend/range regime``, ``volatility regime``, ...): a legitimate
  declaration, refused when the slices are computed until a causal regime model exists
  (REG-007).

The vocabulary is `xq.tracking.slices`: a name outside it is refused when the hypothesis is
registered (C-24, ADR 0055), and again when a registered version's slices are loaded.

Per slice: days (or trades), net P&L, its share of the total net P&L (NaN unless the total is
positive), and the annualized Sharpe ratio and share of positive days (or the mean trade and
win rate).

**R2 gate.** ``max_single_year_pnl_share``: the largest share of the total net P&L earned in one
calendar year must be at most 0.50. It is computed whatever the hypothesis declared, because the
gate itself is pre-registered (``config/gates.yaml``); NaN (a fail) unless the total is positive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yaml
from sqlalchemy import Engine

from xq.core.config import GateCheck, GatesConfig, SessionsConfig
from xq.datasets.calendar_columns import calendar_columns
from xq.tracking.registry import get_experiment, get_hypothesis, get_run, hypothesis_text
from xq.tracking.slices import (
    SESSION,
    VOCABULARY,
    VOLATILITY,
    YEAR,
    SliceError,
    canonical_slice,
    is_regime_slice,
    slice_label,
)

__all__ = [
    "SESSION",
    "VOCABULARY",
    "VOLATILITY",
    "YEAR",
    "DeclaredSlices",
    "SliceError",
    "SliceReport",
    "canonical_slice",
    "declared_slices",
    "is_regime_slice",
    "max_single_year_share",
    "run_slices",
    "session_buckets",
    "slice_label",
    "slice_pnl",
    "volatility_terciles",
    "year_slices",
]

TERCILES = ("low", "mid", "high")
NO_SIGMA = "no_sigma_hat"
OFF_SESSION = "off_session"
_ISSUED = object()


@dataclass(frozen=True)
class DeclaredSlices:
    """The slices a registered hypothesis version declared; built only by the loaders."""

    hypothesis_id: str
    version: int
    yaml_hash: str
    names: tuple[str, ...]
    _issued: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issued is not _ISSUED:
            raise TypeError(
                "slices come only from a registered hypothesis: use declared_slices or run_slices"
            )


def declared_slices(
    engine: Engine, hypothesis_id: str, version: int | None = None
) -> DeclaredSlices:
    """The slices of a registered hypothesis version (default: the latest), from its locked text.

    Raises:
        RegistryError: if the version is not registered.
        SliceError: if a declared slice is unknown or refused.
    """
    ref = get_hypothesis(engine, hypothesis_id, version)
    data = yaml.safe_load(hypothesis_text(engine, hypothesis_id, ref.version))
    raw = (data.get("slices") or []) if isinstance(data, dict) else []
    names = tuple(dict.fromkeys(canonical_slice(str(n)) for n in raw))
    return DeclaredSlices(hypothesis_id, ref.version, ref.yaml_hash, names, _ISSUED)


def run_slices(engine: Engine, run_id: str) -> DeclaredSlices:
    """The slices of the hypothesis version a run's experiment tests."""
    experiment = get_experiment(engine, get_run(engine, run_id).experiment_id)
    return declared_slices(engine, experiment.hypothesis_id, experiment.hypothesis_version)


def year_slices(daily_pnl: pd.Series) -> pd.Series:
    """The calendar year of each trading day."""
    days = pd.to_datetime(pd.Index(daily_pnl.index))
    return pd.Series(days.year.astype(str), index=daily_pnl.index, name=YEAR)


def volatility_terciles(daily_pnl: pd.Series, sigma: pd.Series) -> pd.Series:
    """Low, mid or high sigma-hat tercile of each trading day (module docstring). A day whose
    sigma-hat is not known yet (the estimator's warm-up) is ``no_sigma_hat``; the cut points use
    the days that have one.

    Raises:
        SliceError: if no sliced day has a sigma-hat.
    """
    known = sigma.reindex(daily_pnl.index).to_numpy(np.float64)
    finite = np.isfinite(known)
    if not finite.any():
        raise SliceError("no sliced trading day has a sigma-hat")
    cuts = np.quantile(known[finite], [1 / 3, 2 / 3])
    codes = np.searchsorted(cuts, np.where(finite, known, 0.0), side="right")
    labels = np.where(finite, np.array(TERCILES)[codes], NO_SIGMA)
    return pd.Series(labels, index=daily_pnl.index, name=VOLATILITY)


def session_buckets(times: pd.DatetimeIndex, sessions: SessionsConfig) -> pd.Series:
    """The session bucket of each time (module docstring)."""
    columns = calendar_columns(pd.DatetimeIndex(times), sessions)
    names = list(sessions.sessions)
    overlaps = {frozenset(members): name for name, members in sessions.overlaps.items()}
    inside = np.column_stack([columns[f"in_{n}"].to_numpy(bool) for n in names])
    labels = []
    for row in inside:
        members = [n for n, flag in zip(names, row, strict=True) if flag]
        if not members:
            labels.append(OFF_SESSION)
        elif len(members) == 1:
            labels.append(members[0])
        else:
            labels.append(overlaps.get(frozenset(members), "+".join(members)))
    return pd.Series(labels, index=pd.DatetimeIndex(times), name=SESSION)


@dataclass(frozen=True)
class SliceReport:
    """P&L by each declared slice (module docstring)."""

    declared: DeclaredSlices
    #: Slice name -> one row per bucket.
    tables: dict[str, pd.DataFrame]
    max_year_share: float

    def label(self, name: str) -> str:
        """How the slice's table is labelled: "descriptive", and volatility terciles
        "descriptive, cut ex post"."""
        return slice_label(name)

    def gate_check(self, gates: GatesConfig) -> GateCheck:
        """R2 ``max_single_year_pnl_share``."""
        return gates.criterion("R2", "max_single_year_pnl_share").check(self.max_year_share)


def max_single_year_share(daily_pnl: pd.Series) -> float:
    """Largest share of the total net P&L earned in one calendar year (NaN unless positive)."""
    total = float(daily_pnl.sum())
    if not total > 0:
        return math.nan
    return float(daily_pnl.groupby(year_slices(daily_pnl)).sum().max() / total)


def slice_pnl(
    declared: DeclaredSlices,
    daily_pnl: pd.Series,
    trades: pd.DataFrame,
    *,
    capital: float,
    periods_per_year: int,
    sessions: SessionsConfig,
    sigma: pd.Series | None = None,
) -> SliceReport:
    """Break down net P&L by the declared slices (module docstring).

    Args:
        declared: The slices of the tested hypothesis version (`run_slices`).
        daily_pnl: Net P&L (USD) per trading day, indexed by trading day.
        trades: Holding episodes with ``entry_time``, ``pnl`` and ``open`` (BT-002 trades).
        capital: The capital returns refer to.
        periods_per_year: Annualization of the Sharpe ratio.
        sessions: The session calendar (``config/sessions.yaml``).
        sigma: Daily sigma-hat known at the start of each trading day (for volatility terciles).

    Raises:
        SliceError: for a regime slice (until REG-007), or a volatility slice without any
            sigma-hat.
    """
    regimes = [name for name in declared.names if is_regime_slice(name)]
    if regimes:
        raise SliceError(
            f"slices {regimes} need a causal regime model, which does not exist until REG-007"
        )
    total = float(daily_pnl.sum())
    tables: dict[str, pd.DataFrame] = {}
    for name in declared.names:
        if name == YEAR:
            tables[name] = _daily_table(
                daily_pnl, year_slices(daily_pnl), total, capital, periods_per_year
            )
        elif name == VOLATILITY:
            if sigma is None:
                raise SliceError("a volatility slice needs the daily sigma-hat")
            labels = volatility_terciles(daily_pnl, sigma)
            table = _daily_table(daily_pnl, labels, total, capital, periods_per_year)
            tables[name] = table.reindex([t for t in (*TERCILES, NO_SIGMA) if t in table.index])
        else:
            closed = trades.loc[~trades["open"].astype(bool)] if len(trades) else trades
            tables[name] = _trade_table(closed, sessions, total)
    return SliceReport(declared, tables, max_single_year_share(daily_pnl))


def _daily_table(
    pnl: pd.Series, labels: pd.Series, total: float, capital: float, periods_per_year: int
) -> pd.DataFrame:
    rows = {}
    for bucket, values in pnl.groupby(labels, sort=True):
        r = values.to_numpy(np.float64) / capital
        std = float(np.std(r, ddof=1)) if len(r) > 1 else math.nan
        rows[str(bucket)] = {
            "days": len(r),
            "net_pnl": float(values.sum()),
            "pnl_share": float(values.sum()) / total if total > 0 else math.nan,
            "sharpe": float(np.mean(r)) / std * math.sqrt(periods_per_year)
            if std > 0
            else math.nan,
            "positive_days": float(np.mean(r > 0)),
        }
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("bucket")


def _trade_table(trades: pd.DataFrame, sessions: SessionsConfig, total: float) -> pd.DataFrame:
    columns = ["trades", "net_pnl", "pnl_share", "mean_trade", "win_rate"]
    if trades.empty:
        return pd.DataFrame(columns=columns).rename_axis("bucket")
    labels = session_buckets(pd.DatetimeIndex(trades["entry_time"]), sessions).to_numpy()
    rows = {}
    for bucket, values in trades["pnl"].groupby(labels, sort=True):
        pnl = values.to_numpy(np.float64)
        rows[str(bucket)] = {
            "trades": len(pnl),
            "net_pnl": float(pnl.sum()),
            "pnl_share": float(pnl.sum()) / total if total > 0 else math.nan,
            "mean_trade": float(pnl.mean()),
            "win_rate": float(np.mean(pnl > 0)),
        }
    return pd.DataFrame.from_dict(rows, orient="index", columns=columns).rename_axis("bucket")
