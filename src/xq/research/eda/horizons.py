"""Cost-to-volatility table and the horizon admission list (EDA-006).

For each candidate horizon (a bar timeframe) every adjacent close-to-close return of that timeframe
is one holding period ``[ret_start, ret_end)``, entered at the earlier close and left at the later
one. Its **round-trip cost** in basis points of the entry price, from the configured cost model
(BT-001, `xq.backtest.costs.CostModel`):

- **spread** — the bar's mean quoted spread over its mid close: buying at the ask and selling at
  the bid costs half a spread each way;
- **commission** — both sides' commission of one lot at the entry price, over its notional;
- **slippage** — the model's slippage at entry (``ret_start``) plus at exit (the last instant of
  the period), with sigma-hat the RMS of the last ``horizons.sigma_1m_minutes`` one-minute
  returns completed by the entry (the median of those RMS values where none precede it);
- **financing** — the rollovers inside the period (three on the triple weekday) at the mean of the
  long and short rates (direction-neutral research).

The **expected absolute move** is the mean absolute log return of the timeframe. The ratio is
``mean cost / mean move``, per horizon over all periods and per session and overlap of
``config/sessions.yaml`` (the session the period's bar starts in). A horizon whose overall ratio
exceeds ``horizons.max_cost_to_vol`` (plan default 0.3) is excluded from directional research; it
is still used for execution simulation, realized volatility and entry timing.

While the cost model is provisional, every cost and ratio is a screening figure and carries its
label ("screening, placeholder costs"). The admission list is written to the report
(`admission_yaml`); `write_admission` copies it to ``config/horizons.yaml`` only on explicit
request (``xq research admit-horizons``), and only from a confirmatory run.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import yaml
from matplotlib.figure import Figure

from xq.backtest.costs import CostModel
from xq.core.config import SessionsConfig
from xq.core.errors import XQError
from xq.datasets.calendar_columns import calendar_columns
from xq.research.eda.data import instants_ns
from xq.research.reports import MANIFEST_FILE, RUN_FILE, new_figure

FloatArray = npt.NDArray[np.float64]
ALL_SESSIONS = "all"
ADMISSION_FILE = "admission.yaml"
HORIZONS_CONFIG = "horizons.yaml"
_BPS = 1e4
TABLE_COLUMNS = [
    "horizon",
    "session",
    "n",
    "move_bps",
    "spread_bps",
    "commission_bps",
    "slippage_bps",
    "financing_bps",
    "cost_bps",
    "cost_to_vol",
    "admitted",
    "cost_basis",
]


class AdmissionError(XQError):
    """A horizon admission list cannot be written from this report."""


@dataclass(frozen=True)
class HorizonAdmission:
    """Horizons admitted to directional research, overall and per session."""

    admitted: list[str]
    excluded: list[str]
    by_session: dict[str, list[str]]
    max_cost_to_vol: float
    cost_basis: str
    ratios: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "excluded": self.excluded,
            "max_cost_to_vol": self.max_cost_to_vol,
            "cost_basis": self.cost_basis,
            "cost_to_vol": {h: round(r, 6) for h, r in self.ratios.items()},
            "admitted_by_session": self.by_session,
        }


def trailing_rms(
    ends: npt.NDArray[np.int64], values: FloatArray, at: npt.NDArray[np.int64], window: int
) -> FloatArray:
    """RMS of the last `window` `values` whose `ends` are at or before each `at` (NaN if none).

    `ends` must be sorted.
    """
    cumulative = np.concatenate([[0.0], np.cumsum(values**2)])
    j = np.searchsorted(ends, at, side="right")
    i = np.maximum(j - window, 0)
    count = j - i
    with np.errstate(invalid="ignore", divide="ignore"):
        rms: FloatArray = np.where(
            count > 0, np.sqrt((cumulative[j] - cumulative[i]) / np.maximum(count, 1)), np.nan
        )
    return rms


def rollover_weights(
    cost: CostModel, starts: npt.NDArray[np.int64], ends: npt.NDArray[np.int64]
) -> FloatArray:
    """Financing multipliers of the rollovers in each ``[start, end)`` (3 on the triple weekday)."""
    if len(starts) == 0:
        return np.zeros(0)
    first = pd.Timestamp(int(starts.min()), tz="UTC")
    last = pd.Timestamp(int(ends.max()), tz="UTC")
    rollovers = cost.rollovers(first, last)
    instants = instants_ns(pd.Series(rollovers.index))
    cumulative = np.concatenate([[0.0], np.cumsum(rollovers.to_numpy(dtype=np.float64))])
    upper = cumulative[np.searchsorted(instants, ends, side="left")]
    lower = cumulative[np.searchsorted(instants, starts, side="left")]
    weights: FloatArray = upper - lower
    return weights


def period_costs(
    returns: pd.DataFrame,
    cost: CostModel,
    minute_returns: pd.DataFrame,
    *,
    sigma_minutes: int,
) -> pd.DataFrame:
    """Round-trip cost components (bps) and the absolute move of every holding period."""
    starts = instants_ns(returns["ret_start"])
    ends = instants_ns(returns["ret_end"])
    price = returns["price"].to_numpy(dtype=np.float64)
    minute_ends = instants_ns(minute_returns["ret_end"])
    order = np.argsort(minute_ends, kind="stable")
    sigma = trailing_rms(
        minute_ends[order],
        minute_returns["ret"].to_numpy(dtype=np.float64)[order] * _BPS,
        starts,
        sigma_minutes,
    )
    if np.isnan(sigma).any():
        known = sigma[np.isfinite(sigma)]
        sigma = np.where(np.isnan(sigma), float(np.median(known)) if len(known) else 0.0, sigma)
    contract = float(cost.instrument.contract_size)
    commission = 2 * cost.commission_usd(np.ones(len(price)), price) / (contract * price) * _BPS
    entry = pd.DatetimeIndex(returns["ret_start"])
    exit_ = pd.DatetimeIndex(pd.to_datetime(ends - 1, unit="ns", utc=True))
    slippage = cost.slippage_bps(entry, sigma) + cost.slippage_bps(exit_, sigma)
    financing = cost.config.financing
    rate = (financing.long_rate_annual_pct + financing.short_rate_annual_pct) / 2
    financing_bps = rollover_weights(cost, starts, ends) * rate / 100 / financing.day_count * _BPS
    spread = returns["spread_bps"].to_numpy(dtype=np.float64)
    return pd.DataFrame(
        {
            "move_bps": np.abs(returns["ret"].to_numpy(dtype=np.float64)) * _BPS,
            "spread_bps": spread,
            "commission_bps": commission,
            "slippage_bps": slippage,
            "financing_bps": financing_bps,
            "cost_bps": spread + commission + slippage + financing_bps,
        },
        index=returns.index,
    )


def cost_to_volatility_table(
    returns_by_horizon: Mapping[str, pd.DataFrame],
    minute_returns: pd.DataFrame,
    cost: CostModel,
    sessions: SessionsConfig,
    *,
    max_cost_to_vol: float,
    sigma_minutes: int,
) -> pd.DataFrame:
    """The cost-to-volatility table by horizon and session (module docstring)."""
    rows = []
    names = [*sessions.sessions, *sessions.overlaps]
    for horizon, returns in returns_by_horizon.items():
        if returns.empty:
            continue
        periods = period_costs(returns, cost, minute_returns, sigma_minutes=sigma_minutes)
        inside = calendar_columns(pd.DatetimeIndex(returns["bar_start"]), sessions)
        groups = {ALL_SESSIONS: np.ones(len(returns), dtype=bool)}
        groups |= {name: inside[f"in_{name}"].to_numpy(dtype=bool) for name in names}
        for session, mask in groups.items():
            if not mask.any():
                continue
            chosen = periods.loc[mask]
            move = float(chosen["move_bps"].mean())
            total = float(chosen["cost_bps"].mean())
            ratio = total / move if move > 0 else math.inf
            rows.append(
                {
                    "horizon": horizon,
                    "session": session,
                    "n": int(mask.sum()),
                    "move_bps": move,
                    "spread_bps": float(chosen["spread_bps"].mean()),
                    "commission_bps": float(chosen["commission_bps"].mean()),
                    "slippage_bps": float(chosen["slippage_bps"].mean()),
                    "financing_bps": float(chosen["financing_bps"].mean()),
                    "cost_bps": total,
                    "cost_to_vol": ratio,
                    "admitted": ratio <= max_cost_to_vol,
                    "cost_basis": cost.result_label,
                }
            )
    return pd.DataFrame(rows, columns=TABLE_COLUMNS)


def admission(
    table: pd.DataFrame, candidates: Sequence[str], max_cost_to_vol: float
) -> HorizonAdmission:
    """The admission list: candidates whose overall ratio is at most `max_cost_to_vol`.

    A candidate without data is excluded (it cannot be shown to be affordable).
    """
    overall = table.loc[table["session"] == ALL_SESSIONS].set_index("horizon")
    column = overall["cost_to_vol"]
    ratios = {h: float(column[h]) for h in candidates if h in overall.index}
    admitted = [h for h in candidates if h in ratios and ratios[h] <= max_cost_to_vol]
    by_session: dict[str, list[str]] = {}
    for session, chunk in table.loc[table["session"] != ALL_SESSIONS].groupby("session", sort=True):
        ok = set(chunk.loc[chunk["admitted"].astype(bool), "horizon"])
        by_session[str(session)] = [h for h in candidates if h in ok]
    basis = str(table["cost_basis"].iloc[0]) if len(table) else "no data"
    return HorizonAdmission(
        admitted=admitted,
        excluded=[h for h in candidates if h not in admitted],
        by_session=by_session,
        max_cost_to_vol=max_cost_to_vol,
        cost_basis=basis,
        ratios=ratios,
    )


def admission_yaml(result: HorizonAdmission, provenance: Mapping[str, Any]) -> str:
    """The admission list as YAML, with where it came from."""
    header = (
        "# Horizon admission list (EDA-006): horizons whose round-trip cost is at most\n"
        "# max_cost_to_vol of their expected absolute move. Generated; do not edit by hand.\n"
    )
    body = {**result.as_dict(), "provenance": dict(provenance)}
    return header + yaml.safe_dump(body, sort_keys=False)


def write_admission(report_dir: Path, config_dir: Path) -> Path:
    """Copy a report's admission list to ``<config_dir>/horizons.yaml``.

    Raises:
        AdmissionError: if the report has no admission list or run record, the admission list
            differs from the report manifest, or the run was not confirmatory.
    """
    proposal = report_dir / ADMISSION_FILE
    run_file = report_dir / RUN_FILE
    manifest_file = report_dir / MANIFEST_FILE
    for path in (proposal, run_file, manifest_file):
        if not path.is_file():
            raise AdmissionError(f"{path} does not exist: not an EDA report")
    run = json.loads(run_file.read_text(encoding="utf-8"))
    if not run.get("confirmatory", False):
        raise AdmissionError(
            f"run {run.get('run_id')} was exploratory; only a confirmatory run's admission list "
            "may be written to the configuration"
        )
    expected = json.loads(manifest_file.read_text(encoding="utf-8"))["files"].get(ADMISSION_FILE)
    actual = hashlib.sha256(proposal.read_bytes()).hexdigest()
    if expected != actual:
        raise AdmissionError(f"{proposal} differs from the report manifest")
    target = config_dir / HORIZONS_CONFIG
    text = proposal.read_text(encoding="utf-8")
    source = f"# Copied from {report_dir} (run {run['run_id']}).\n"
    target.write_text(source + text, encoding="utf-8")
    return target


def cost_to_volatility_figure(table: pd.DataFrame, max_cost_to_vol: float, title: str) -> Figure:
    """Overall cost-to-volatility ratio per horizon against the admission bound."""
    overall = table.loc[table["session"] == ALL_SESSIONS]
    figure = new_figure(7, 4)
    axes = figure.subplots()
    x = np.arange(len(overall))
    axes.bar(x, overall["cost_to_vol"].to_numpy(dtype=np.float64))
    axes.axhline(max_cost_to_vol, linestyle="--", linewidth=1, label=f"bound {max_cost_to_vol:g}")
    axes.set_xticks(x, overall["horizon"].to_numpy())
    axes.set_yscale("log")
    axes.set_ylabel("round-trip cost / mean |return|")
    axes.legend()
    figure.suptitle(title)
    return figure
