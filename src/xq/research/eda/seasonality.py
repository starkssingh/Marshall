"""Seasonality, session and event-window effects with corrected intervals (EDA-004).

An **effect** is the mean of a variable in a bucket minus its overall mean (``effect``), also in
units of the variable's overall standard deviation (``effect_sd``). Families of buckets:

- ``hour_of_week`` — New York hour of the week of the bar start (Monday 00:00 = 0, the convention
  of the spread statistics), on ``seasonality.intraday_timeframe`` bars;
- ``day_of_week`` and ``month`` — of the trading day, on daily bars;
- ``session`` — in each session and overlap of ``config/sessions.yaml`` versus overall (a bar
  belongs to a session when it starts inside it; sessions overlap, so these buckets do too);
- ``event_window`` — in each event window (the dataset's own, ``config/sessions.yaml``, plus
  ``seasonality.event_windows``: the LBMA auctions) versus overall, on
  ``seasonality.event_timeframe`` bars.

Variables: the log return and the absolute log return (bps; adjacent returns only), the tick count
and the mean spread (bps of the mid close).

**Uncertainty.** Observations of one trading week share volatility, so the standard error of a
bucket mean is cluster-robust — by trading week, or by calendar month for ``month``:
``se = sqrt(G / (G - 1) * sum_c (sum_{i in c} (v_i - mean_b))^2) / n_b`` over the G clusters with
observations in the bucket (the overall mean's own, shared and much smaller, uncertainty is
ignored). The interval is ``effect +- c * se`` with c the Student-t quantile with ``G - 1``
degrees of freedom at ``1 - alpha / (2 m)``, m the number of buckets of the family (Bonferroni),
so all intervals of a family hold together with probability at least ``1 - alpha``; the t
quantile keeps buckets with few clusters (a short sample, a month seen in two years) from looking
precise. ``significant`` means the interval excludes zero (and the standard error is positive).

**Split-half stability.** The trading days are split at the median into a first and a second
half; the effect is re-estimated in each half against that half's overall mean. An effect is
``stable`` when both halves have the same sign and their difference is within the corrected
interval of a difference (``|e1 - e2| <= c * sqrt(se1^2 + se2^2)``, c with the smaller half's
clusters); otherwise ``unstable``; ``insufficient data`` when a half has fewer than two
clusters. Unstable effects are labelled as
such and are not a basis for hypotheses. All of it is descriptive: seasonality must be validated
out of sample (project instructions, section 4).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date

import numpy as np
import numpy.typing as npt
import pandas as pd
from matplotlib.figure import Figure
from scipy.stats import t as student_t

from xq.core.config import SessionsConfig
from xq.datasets.calendar_columns import calendar_columns
from xq.research.reports import new_figure

FloatArray = npt.NDArray[np.float64]
STABLE, UNSTABLE, INSUFFICIENT = "stable", "unstable", "insufficient data"
EFFECT_COLUMNS = [
    "family",
    "variable",
    "bucket",
    "n",
    "clusters",
    "mean",
    "effect",
    "effect_sd",
    "se",
    "ci_low",
    "ci_high",
    "significant",
    "effect_first_half",
    "effect_second_half",
    "stability",
]
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def cluster_se(values: FloatArray, clusters: npt.NDArray[np.int64]) -> float:
    """Cluster-robust standard error of the mean of `values` (NaN with fewer than 2 clusters)."""
    if len(values) == 0:
        return math.nan
    labels, inverse = np.unique(clusters, return_inverse=True)
    groups = len(labels)
    if groups < 2:
        return math.nan
    sums = np.bincount(inverse, weights=values - values.mean(), minlength=groups)
    return math.sqrt(groups / (groups - 1) * float(sums @ sums)) / len(values)


def critical_value(level: float, clusters: int) -> float:
    """Two-sided critical value at `level` for a mean with `clusters` clusters: Student-t with
    ``clusters - 1`` degrees of freedom (NaN with fewer than two clusters)."""
    if clusters < 2:
        return math.nan
    return float(student_t.ppf(1 - level / 2, clusters - 1))


def effects(
    values: pd.Series,
    members: pd.DataFrame,
    clusters: pd.Series,
    second_half: pd.Series,
    *,
    alpha: float,
    family: str,
    variable: str,
) -> pd.DataFrame:
    """Effects of the buckets in `members` (boolean columns, one per bucket) on `values`.

    All arguments share one index. Rows with a missing value are ignored. See the module docstring.
    """
    v = values.to_numpy(dtype=np.float64)
    valid = np.isfinite(v)
    v = v[valid]
    groups = clusters.to_numpy(dtype=np.int64)[valid]
    late = second_half.to_numpy(dtype=bool)[valid]
    masks = {str(c): members[c].to_numpy(dtype=bool)[valid] for c in members.columns}
    overall_sd = float(np.std(v, ddof=1)) if len(v) > 1 else math.nan
    m = max(1, sum(1 for mask in masks.values() if mask.sum() > 0))
    level = alpha / m
    halves = [~late, late]
    half_means = [float(v[h].mean()) if h.any() else math.nan for h in halves]
    rows = []
    for bucket, mask in masks.items():
        n = int(mask.sum())
        if n == 0:
            continue
        mean = float(v[mask].mean())
        effect = mean - float(v.mean())
        se = cluster_se(v[mask], groups[mask])
        count = len(np.unique(groups[mask]))
        width = critical_value(level, count) * se
        parts = []
        for h, half_mean in zip(halves, half_means, strict=True):
            inside = mask & h
            if inside.sum() == 0:
                parts.append((math.nan, math.nan, 0))
                continue
            parts.append(
                (
                    float(v[inside].mean()) - half_mean,
                    cluster_se(v[inside], groups[inside]),
                    len(np.unique(groups[inside])),
                )
            )
        (e1, se1, g1), (e2, se2, g2) = parts
        rows.append(
            {
                "family": family,
                "variable": variable,
                "bucket": bucket,
                "n": n,
                "clusters": count,
                "mean": mean,
                "effect": effect,
                "effect_sd": effect / overall_sd if overall_sd > 0 else math.nan,
                "se": se,
                "ci_low": effect - width,
                "ci_high": effect + width,
                # A zero standard error (identical values) cannot measure uncertainty.
                "significant": bool(math.isfinite(width) and se > 0 and abs(effect) > width),
                "effect_first_half": e1,
                "effect_second_half": e2,
                "stability": _stability(e1, se1, e2, se2, critical_value(level, min(g1, g2))),
            }
        )
    return pd.DataFrame(rows, columns=EFFECT_COLUMNS)


def _stability(e1: float, se1: float, e2: float, se2: float, critical: float) -> str:
    if not all(math.isfinite(x) for x in (e1, se1, e2, se2, critical)):
        return INSUFFICIENT
    same_sign = np.sign(e1) == np.sign(e2) and e1 != 0
    consistent = abs(e1 - e2) <= critical * math.sqrt(se1**2 + se2**2)
    return STABLE if same_sign and consistent else UNSTABLE


def one_hot(labels: pd.Series, order: list[str] | None = None) -> pd.DataFrame:
    """Boolean membership columns of a label series, in `order` (default: sorted labels)."""
    names = order if order is not None else sorted({str(x) for x in labels.dropna()})
    text = labels.astype("string")
    return pd.DataFrame(
        {name: (text == name).fillna(False).to_numpy() for name in names}, index=labels.index
    )


def hour_of_week_labels(starts: pd.Series) -> pd.Series:
    """``"Mon 09"``-style New York hour of the week of each instant (Monday 00:00 first)."""
    local = pd.DatetimeIndex(starts).tz_convert("America/New_York")
    labels = [f"{_DAYS[d]} {h:02d}" for d, h in zip(local.weekday, local.hour, strict=True)]
    return pd.Series(labels, index=starts.index, dtype="string")


def hour_of_week_order() -> list[str]:
    """Every hour-of-week label in week order."""
    return [f"{day} {hour:02d}" for day in _DAYS for hour in range(24)]


def week_clusters(days: pd.Series) -> pd.Series:
    """Cluster id of each trading day's ISO week (``year * 100 + week``)."""
    return pd.Series(
        [d.isocalendar()[0] * 100 + d.isocalendar()[1] for d in days], index=days.index
    )


def month_clusters(days: pd.Series) -> pd.Series:
    """Cluster id of each trading day's calendar month (``year * 100 + month``)."""
    return pd.Series([d.year * 100 + d.month for d in days], index=days.index)


def split_halves(days: pd.Series) -> pd.Series:
    """True for trading days in the second half (at or after the median distinct day)."""
    distinct = sorted(set(days))
    if not distinct:
        return pd.Series(dtype=bool, index=days.index)
    cut: date = distinct[len(distinct) // 2]
    return pd.Series([d >= cut for d in days], index=days.index, dtype=bool)


def windows_config(sessions: SessionsConfig, extra: Mapping[str, object]) -> SessionsConfig:
    """`sessions` with the extra event windows of ``config/eda.yaml`` added."""
    return sessions.model_copy(update={"event_windows": {**sessions.event_windows, **extra}})


def membership(starts: pd.Series, sessions: SessionsConfig) -> pd.DataFrame:
    """Session, overlap and event-window membership of each bar start (calendar columns)."""
    columns = calendar_columns(pd.DatetimeIndex(starts), sessions)
    names = [*sessions.sessions, *sessions.overlaps]
    frame = {name: columns[f"in_{name}"].to_numpy(dtype=bool) for name in names}
    frame |= {
        name: columns[f"in_{name}_window"].to_numpy(dtype=bool)
        for name in sessions.event_windows
        if f"in_{name}_window" in columns
    }
    return pd.DataFrame(frame, index=starts.index)


def variables(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """The EDA-004 variables of a returns frame (`xq.research.eda.data.bar_returns`)."""
    ret = frame["ret"] * 1e4
    return {
        "ret_bps": ret,
        "abs_ret_bps": ret.abs(),
        "tick_count": frame["tick_count"].astype(np.float64),
        "spread_bps": frame["spread_bps"],
    }


def family_effects(
    frame: pd.DataFrame,
    members: pd.DataFrame,
    clusters: pd.Series,
    *,
    alpha: float,
    family: str,
) -> pd.DataFrame:
    """Effects of every variable of `frame` over the buckets in `members`."""
    halves = split_halves(frame["trading_day"])
    tables = [
        effects(values, members, clusters, halves, alpha=alpha, family=family, variable=name)
        for name, values in variables(frame).items()
    ]
    return pd.concat(tables, ignore_index=True)


def day_of_week_members(days: pd.Series) -> pd.DataFrame:
    labels = pd.Series([_DAYS[d.weekday()] for d in days], index=days.index, dtype="string")
    return one_hot(labels, [d for d in _DAYS if (labels == d).any()])


def month_members(days: pd.Series) -> pd.DataFrame:
    labels = pd.Series([_MONTHS[d.month - 1] for d in days], index=days.index, dtype="string")
    return one_hot(labels, [m for m in _MONTHS if (labels == m).any()])


def effects_figure(table: pd.DataFrame, variables_: list[str], title: str) -> Figure:
    """Effects with corrected intervals, one panel per variable; unstable effects hollow."""
    figure = new_figure(12, 3.2 * len(variables_))
    grid = figure.subplots(len(variables_), 1, squeeze=False)
    for axes, variable in zip(grid[:, 0], variables_, strict=True):
        chunk = table.loc[table["variable"] == variable]
        x = np.arange(len(chunk))
        effect = chunk["effect"].to_numpy(dtype=np.float64)
        low = effect - chunk["ci_low"].to_numpy(dtype=np.float64)
        high = chunk["ci_high"].to_numpy(dtype=np.float64) - effect
        axes.errorbar(x, effect, yerr=[low, high], fmt="none", linewidth=0.8)
        stable = (chunk["stability"] == STABLE).to_numpy()
        axes.plot(x[stable], effect[stable], "o", markersize=3, label="stable")
        axes.plot(
            x[~stable], effect[~stable], "o", markersize=3, fillstyle="none", label="not stable"
        )
        axes.axhline(0, linewidth=0.5)
        step = max(1, len(chunk) // 24)
        axes.set_xticks(
            x[::step], chunk["bucket"].to_numpy()[::step], rotation=90, fontsize="small"
        )
        axes.set_ylabel(f"{variable} effect")
        axes.legend(fontsize="small")
    figure.suptitle(title)
    return figure
