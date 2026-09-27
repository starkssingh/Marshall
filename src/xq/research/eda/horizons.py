"""Cost-to-volatility table and the horizon admission list (EDA-006, ADR 0037, ADR 0040).

**Holding periods are TGT-002's.** Decisions are the 1m bars whose availability falls on the
``horizons.decision_step`` grid (5 minutes). Each 1m bar contributes one quote, its closing mid and
spread, stamped just before the bar's end (the close is the last quote of the bar). For every
candidate horizon label h (``15m``, ``1d`` = one trading day, `market_horizon`), the move is
computed by TGT-002's own `xq.targets.returns.compute` (mid variant) on those quotes, with the
execution latency and allowed fill delay of the ``horizons.target_set`` target set: entry at the
first quote at or after t + latency of market time, exit at the first at or after t + h + latency,
no measurement for a decision taken while the market is closed or when a fill comes later than the
allowed delay, and ``crosses_close`` when a close lies between the fills. Periods overlap (a
decision every 5 minutes); that is fine for means, and n is reported.

Its **round-trip cost** in basis points, from the configured cost model (BT-001,
`xq.backtest.costs.CostModel`):

- **spread** — half the quoted spread at the entry fill plus half the spread at the exit fill, each
  over its mid: the closing spread of the 1m bar whose close is the fill (buying at the ask and
  selling at the bid costs half a spread each way, at the instants the trade happens);
- **commission** — both sides' commission of one lot at the entry mid, over its notional;
- **slippage** — the model's slippage at the entry fill and at the exit fill, with sigma-hat the RMS
  of the last ``horizons.sigma_1m_minutes`` one-minute returns completed by the entry. Where no
  one-minute return precedes the entry, sigma-hat is the median of the sigma-hats of the periods
  entered earlier (an expanding median, so nothing later is used); a period with no earlier one is
  dropped;
- **financing** — the rollovers between the fills (three on the triple weekday) at the mean of the
  long and short rates (direction-neutral research).

The **expected absolute move** is the mean absolute log mid move. The ratio is ``mean cost / mean
move``, per horizon over all periods and per session and overlap of ``config/sessions.yaml`` (the
session the decision falls in). The median absolute move, the median cost and their ratio
``median cost / median move`` are reported beside them — heavy tails pull the mean move above the
typical one — but admission uses the mean ratio. A horizon whose overall mean ratio exceeds
``horizons.max_cost_to_vol`` (plan default 0.3) is excluded from directional research; it is
still used for execution simulation, realized volatility and entry timing. The per-session rows
are report-only (``below_bound`` marks a ratio at most the bound): a session-restricted horizon
can be used only through a pre-registered hypothesis, never through the admission list (ADR 0041).

While the cost model is provisional, every cost and ratio is a screening figure and carries its
label ("screening, placeholder costs"). The admission list is written to the report
(`admission_yaml`, with the cost basis and whether the costs are provisional);
`write_admission` copies it to ``config/horizons.yaml`` only on explicit request
(``xq research admit-horizons``), only from a confirmatory run's unaltered report, and — while
the costs are placeholders — only with ``--allow-placeholder-costs``; the written file records
the cost basis and that flag.
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
from xq.core.types import PriceBasis, Timeframe
from xq.data.calendar import MarketClock
from xq.datasets.calendar_columns import calendar_columns
from xq.research.eda.data import AVAILABLE, BAR_START, instants_ns
from xq.research.reports import MANIFEST_FILE, RUN_FILE, new_figure
from xq.targets.base import TargetSpec, market_horizon
from xq.targets.returns import ForwardReturnParams
from xq.targets.returns import compute as forward_return

FloatArray = npt.NDArray[np.float64]
ALL_SESSIONS = "all"
ADMISSION_FILE = "admission.yaml"
HORIZONS_CONFIG = "horizons.yaml"
_BPS = 1e4
PERIOD_COLUMNS = [
    "label_start",
    "label_end",
    "crosses_close",
    "move",
    "entry_mid",
    "exit_mid",
    "entry_spread",
    "exit_spread",
]
TABLE_COLUMNS = [
    "horizon",
    "session",
    "n",
    "crosses_close_share",
    "move_bps",
    "move_median_bps",
    "spread_bps",
    "commission_bps",
    "slippage_bps",
    "financing_bps",
    "cost_bps",
    "cost_median_bps",
    "cost_to_vol",
    "cost_to_vol_median",
    "below_bound",
    "cost_basis",
]


class AdmissionError(XQError):
    """A horizon admission list cannot be written from this report."""


@dataclass(frozen=True)
class HorizonAdmission:
    """Horizons admitted to directional research: on their overall ratio only (ADR 0041)."""

    admitted: list[str]
    excluded: list[str]
    max_cost_to_vol: float
    cost_basis: str
    provisional_costs: bool
    ratios: dict[str, float] = field(default_factory=dict)
    #: Median cost / median move per horizon: reported, never used to admit.
    median_ratios: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "excluded": self.excluded,
            "max_cost_to_vol": self.max_cost_to_vol,
            "cost_basis": self.cost_basis,
            "provisional_costs": self.provisional_costs,
            "cost_to_vol": {h: round(r, 6) for h, r in self.ratios.items()},
            "cost_to_vol_median": {h: round(r, 6) for h, r in self.median_ratios.items()},
        }


def minute_quotes(bars: pd.DataFrame, basis: PriceBasis | str = PriceBasis.MID) -> pd.DataFrame:
    """One quote per complete 1m bar: its closing mid and spread, stamped just before its end.

    `bars` are 1m bars of price `basis` (``close``, ``spread_close``, sorted by ``bar_start_utc``).
    The close is the last quote of the bar, so its stamp is the last instant of the bar: a fill
    "at or after" an instant never uses a bar that ended before it.
    """
    starts = instants_ns(bars[BAR_START])
    close = bars["close"].to_numpy(dtype=np.float64)
    spread = bars["spread_close"].to_numpy(dtype=np.float64)
    chosen = PriceBasis(basis)
    if chosen is PriceBasis.BID:
        mid = close + spread / 2
    elif chosen is PriceBasis.ASK:
        mid = close - spread / 2
    else:
        mid = close
    return pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(starts + Timeframe.M1.nanos - 1, unit="ns", utc=True),
            "bid": mid - spread / 2,
            "ask": mid + spread / 2,
            "mid": mid,
            "spread_close": spread,
        }
    )


def decision_times(bars: pd.DataFrame, step: pd.Timedelta) -> pd.DatetimeIndex:
    """Availability of the 1m bars whose end lies on the `step` grid (multiples of it in UTC)."""
    ends = instants_ns(bars[BAR_START]) + Timeframe.M1.nanos
    on_grid = ends % step.value == 0
    return pd.DatetimeIndex(bars.loc[on_grid, AVAILABLE], name="decision_time")


def holding_periods(
    bars: pd.DataFrame,
    label: str,
    *,
    trading_day: pd.Timedelta,
    params: ForwardReturnParams,
    step: pd.Timedelta,
    clock: MarketClock,
    basis: PriceBasis | str = PriceBasis.MID,
) -> pd.DataFrame:
    """TGT-002 holding periods of horizon `label` from the 1m bars (module docstring).

    Returns:
        One row per measured decision (indexed by decision time): the fills' instants
        (``label_start``, ``label_end``), ``crosses_close``, the log mid ``move``, and the mid and
        spread quoted at the entry and exit fills.
    """
    quotes = minute_quotes(bars, basis)
    decisions = decision_times(bars, step)
    spec = TargetSpec(
        f"eda_move_{label}",
        market_horizon(label, trading_day),
        "mid",
        {
            "execution_latency_ms": params.execution_latency_ms,
            "max_fill_delay_s": params.max_fill_delay_s,
            "normalized": False,
        },
    )
    sigma = pd.Series(np.nan, index=decisions)
    moves = forward_return(spec, quotes.loc[:, ["ts_utc", "bid", "ask"]], sigma, clock)
    moves = moves.loc[moves["value"].notna()]
    stamps = instants_ns(quotes["ts_utc"])
    entry = np.searchsorted(stamps, instants_ns(moves["label_start"]))
    exit_ = np.searchsorted(stamps, instants_ns(moves["label_end"]))
    mid = quotes["mid"].to_numpy()
    spread = quotes["spread_close"].to_numpy()
    return pd.DataFrame(
        {
            "label_start": moves["label_start"],
            "label_end": moves["label_end"],
            "crosses_close": moves["crosses_close"].to_numpy(dtype=bool),
            "move": moves["value"].to_numpy(dtype=np.float64),
            "entry_mid": mid[entry],
            "exit_mid": mid[exit_],
            "entry_spread": spread[entry],
            "exit_spread": spread[exit_],
        },
        index=moves.index,
    )


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


def causal_fill(
    values: FloatArray, times: npt.NDArray[np.int64]
) -> tuple[FloatArray, npt.NDArray[np.bool_]]:
    """Fill missing `values` with the median of the values at strictly earlier `times`.

    Returns the filled values and which ones are usable: a missing value with no earlier value is
    not filled (and not usable). Nothing at or after a value's own time is used for it.
    """
    order = np.argsort(times, kind="stable")
    ordered = pd.Series(values[order])
    ordered_times = times[order]
    # The median of every non-missing value up to each position, then taken at the last position
    # with a strictly earlier time.
    running = ordered.expanding().median().to_numpy(dtype=np.float64)
    earlier = np.searchsorted(ordered_times, ordered_times, side="left") - 1
    prior = np.where(earlier >= 0, running[np.maximum(earlier, 0)], np.nan)
    filled_ordered = np.where(np.isnan(ordered.to_numpy()), prior, ordered.to_numpy())
    filled = np.empty(len(values))
    filled[order] = filled_ordered
    return filled, np.isfinite(filled)


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
    periods: pd.DataFrame,
    cost: CostModel,
    minute_returns: pd.DataFrame,
    *,
    sigma_minutes: int,
) -> pd.DataFrame:
    """Round-trip cost components (bps) and the absolute move of every holding period.

    Periods without a sigma-hat (none can be known at their entry) are left out (module
    docstring).
    """
    starts = instants_ns(periods["label_start"])
    ends = instants_ns(periods["label_end"])
    minute_ends = instants_ns(minute_returns["ret_end"])
    order = np.argsort(minute_ends, kind="stable")
    sigma = trailing_rms(
        minute_ends[order],
        minute_returns["ret"].to_numpy(dtype=np.float64)[order] * _BPS,
        starts,
        sigma_minutes,
    )
    sigma, usable = causal_fill(sigma, starts)
    periods = periods.loc[usable]
    starts, ends, sigma = starts[usable], ends[usable], sigma[usable]
    price = periods["entry_mid"].to_numpy(dtype=np.float64)
    contract = float(cost.instrument.contract_size)
    commission = 2 * cost.commission_usd(np.ones(len(price)), price) / (contract * price) * _BPS
    slippage = cost.slippage_bps(
        pd.DatetimeIndex(periods["label_start"]), sigma
    ) + cost.slippage_bps(pd.DatetimeIndex(periods["label_end"]), sigma)
    financing = cost.config.financing
    rate = (financing.long_rate_annual_pct + financing.short_rate_annual_pct) / 2
    financing_bps = rollover_weights(cost, starts, ends) * rate / 100 / financing.day_count * _BPS
    spread = (
        periods["entry_spread"].to_numpy(dtype=np.float64) / price
        + periods["exit_spread"].to_numpy(dtype=np.float64)
        / periods["exit_mid"].to_numpy(dtype=np.float64)
    ) * (_BPS / 2)
    return pd.DataFrame(
        {
            "move_bps": np.abs(periods["move"].to_numpy(dtype=np.float64)) * _BPS,
            "spread_bps": spread,
            "commission_bps": commission,
            "slippage_bps": slippage,
            "financing_bps": financing_bps,
            "cost_bps": spread + commission + slippage + financing_bps,
            "crosses_close": periods["crosses_close"].to_numpy(dtype=bool),
        },
        index=periods.index,
    )


def cost_to_volatility_table(
    periods_by_horizon: Mapping[str, pd.DataFrame],
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
    for horizon, periods in periods_by_horizon.items():
        if periods.empty:
            continue
        costs = period_costs(periods, cost, minute_returns, sigma_minutes=sigma_minutes)
        if costs.empty:
            continue
        inside = calendar_columns(pd.DatetimeIndex(costs.index), sessions)
        groups = {ALL_SESSIONS: np.ones(len(costs), dtype=bool)}
        groups |= {name: inside[f"in_{name}"].to_numpy(dtype=bool) for name in names}
        for session, mask in groups.items():
            if not mask.any():
                continue
            chosen = costs.loc[mask]
            move = float(chosen["move_bps"].mean())
            total = float(chosen["cost_bps"].mean())
            ratio = total / move if move > 0 else math.inf
            move_median = float(chosen["move_bps"].median())
            cost_median = float(chosen["cost_bps"].median())
            ratio_median = cost_median / move_median if move_median > 0 else math.inf
            rows.append(
                {
                    "horizon": horizon,
                    "session": session,
                    "n": int(mask.sum()),
                    "crosses_close_share": float(chosen["crosses_close"].mean()),
                    "move_bps": move,
                    "move_median_bps": move_median,
                    "spread_bps": float(chosen["spread_bps"].mean()),
                    "commission_bps": float(chosen["commission_bps"].mean()),
                    "slippage_bps": float(chosen["slippage_bps"].mean()),
                    "financing_bps": float(chosen["financing_bps"].mean()),
                    "cost_bps": total,
                    "cost_median_bps": cost_median,
                    "cost_to_vol": ratio,
                    "cost_to_vol_median": ratio_median,
                    "below_bound": ratio <= max_cost_to_vol,
                    "cost_basis": cost.result_label,
                }
            )
    return pd.DataFrame(rows, columns=TABLE_COLUMNS)


def admission(
    table: pd.DataFrame,
    candidates: Sequence[str],
    max_cost_to_vol: float,
    *,
    provisional_costs: bool,
) -> HorizonAdmission:
    """The admission list: candidates whose overall ratio is at most `max_cost_to_vol`.

    A candidate without data is excluded (it cannot be shown to be affordable). Only the overall
    ratios admit: the per-session rows are report-only (ADR 0041). `provisional_costs` says
    whether the table was priced with a provisional cost model.
    """
    overall = table.loc[table["session"] == ALL_SESSIONS].set_index("horizon")
    column = overall["cost_to_vol"]
    ratios = {h: float(column[h]) for h in candidates if h in overall.index}
    medians = overall["cost_to_vol_median"]
    median_ratios = {h: float(medians[h]) for h in candidates if h in overall.index}
    admitted = [h for h in candidates if h in ratios and ratios[h] <= max_cost_to_vol]
    basis = str(table["cost_basis"].iloc[0]) if len(table) else "no data"
    return HorizonAdmission(
        admitted=admitted,
        excluded=[h for h in candidates if h not in admitted],
        max_cost_to_vol=max_cost_to_vol,
        cost_basis=basis,
        provisional_costs=provisional_costs,
        ratios=ratios,
        median_ratios=median_ratios,
    )


def admission_yaml(result: HorizonAdmission, provenance: Mapping[str, Any]) -> str:
    """The admission list as YAML, with where it came from."""
    header = (
        "# Horizon admission list (EDA-006): horizons whose round-trip cost is at most\n"
        "# max_cost_to_vol of their expected absolute move. Generated; do not edit by hand.\n"
    )
    body = {**result.as_dict(), "provenance": dict(provenance)}
    return header + yaml.safe_dump(body, sort_keys=False)


def write_admission(
    report_dir: Path, config_dir: Path, *, allow_placeholder_costs: bool = False
) -> Path:
    """Copy a report's admission list to ``<config_dir>/horizons.yaml``.

    The written file is the report's list plus ``allow_placeholder_costs`` and where it came from
    (the list already carries its ``cost_basis`` and ``provisional_costs``).

    Raises:
        AdmissionError: if the report has no admission list or run record, the run was not
            confirmatory, the admission list differs from the report manifest, or the list was
            priced with provisional (placeholder) costs and `allow_placeholder_costs` is False.
            A list that does not say whether its costs were provisional counts as provisional.
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
    listed: dict[str, Any] = yaml.safe_load(proposal.read_text(encoding="utf-8"))
    provisional = bool(listed.get("provisional_costs", True))
    if provisional and not allow_placeholder_costs:
        raise AdmissionError(
            f"the admission list was priced with provisional costs "
            f"({listed.get('cost_basis', 'cost basis not recorded')}); pass "
            "--allow-placeholder-costs to write it to the configuration anyway"
        )
    written = {
        **listed,
        "allow_placeholder_costs": allow_placeholder_costs,
        "source": {"report": str(report_dir), "run_id": run["run_id"]},
    }
    target = config_dir / HORIZONS_CONFIG
    header = (
        "# Horizon admission list (EDA-006), copied from an EDA report by\n"
        "# `xq research admit-horizons`. Generated; do not edit by hand.\n"
    )
    target.write_text(header + yaml.safe_dump(written, sort_keys=False), encoding="utf-8")
    return target


def cost_to_volatility_figure(table: pd.DataFrame, max_cost_to_vol: float, title: str) -> Figure:
    """Overall cost-to-volatility ratio per horizon against the admission bound."""
    overall = table.loc[table["session"] == ALL_SESSIONS]
    figure = new_figure(7, 4)
    axes = figure.subplots()
    x = np.arange(len(overall))
    axes.bar(x, overall["cost_to_vol"].to_numpy(dtype=np.float64), label="mean cost / mean move")
    axes.plot(
        x,
        overall["cost_to_vol_median"].to_numpy(dtype=np.float64),
        "o",
        label="median cost / median move",
    )
    axes.axhline(max_cost_to_vol, linestyle="--", linewidth=1, label=f"bound {max_cost_to_vol:g}")
    axes.set_xticks(x, overall["horizon"].to_numpy())
    axes.set_yscale("log")
    axes.set_ylabel("round-trip cost / mean |move|")
    axes.legend()
    figure.suptitle(title)
    return figure
