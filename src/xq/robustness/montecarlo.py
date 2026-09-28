"""Monte Carlo equity with the risk rules applied (ROB-004).

A backtest shows one path. The risk budget (0.5 % per trade, the drawdown throttle, the daily-loss,
drawdown and cooldown halts) must hold on the paths the strategy *could* have had. This module
resamples the strategy's trade outcomes and replays every resampled path through the **real risk
engine**: `RiskEngine.evaluate` sizes or refuses every entry and `RiskStateTracker` keeps the
risk state, exactly as in the event backtester.

**Trade outcomes in risk units.** Each closed trade becomes an R-multiple:

    R = trade return / (stop_sigmas x sigma-hat at entry)

The trade return is its net P&L over the notional at entry, so costs are already in R. The
denominator is the stop a strategy places at ``stop_sigmas`` (3) daily sigma-hats, the event
tier's default. R is taken as observed, so a loss beyond the stop (a gap, or a strategy without
stops) keeps its size.

**Paths.**

- A stationary bootstrap of the sequence of R-multiples, with a Politis-White mean block of at
  least ``min_block_trades``, keeps clusters of losses.
- The **calendar** of the trades (their entry and exit times, direction, price, spread and
  sigma-hat) is kept, and only the outcomes are resampled. Daily limits, cooldowns and trades per
  day therefore meet the timing the strategy really had.

**Replay.** For each trade of a path:

1. the tracker rolls the trading day (the previous day's closing equity is the next day's start);
2. the equity is observed at the entry, and a market intent in the trade's direction goes to
   `RiskEngine.evaluate`. It carries the trade's stop (``stop_sigmas`` sigma-hats from the entry
   side) and requests the most exposure the profile allows, so the risk budget, not the request,
   sizes it;
3. an approved size fills at the entry, and exits at the price that realizes the trade's R.
   Commission is zero, because costs are already in R;
4. the equity is observed at the exit.

A refused entry (a halt, a cooldown, a stop the policy rejects, or a size rounded to zero) skips
the trade.

**Reported per path:**

- the maximum drawdown, with the starting capital as the first peak (BT-003's definition);
- the final equity over the capital;
- whether the drawdown halt fired (an entry refused by it, or the worst drawdown at the halt
  level);
- whether the equity ever fell to ``ruin_level`` of the capital (ruin);
- how many entries were taken and refused.

The distribution gives the drawdown quantiles, the probability of hitting the halt level and the
ruin probability.

**R2 gate.** ``monte_carlo_drawdown``: the ``quantile`` (95 %) of the paths' maximum drawdowns
must be below ``below`` (0.15, the halt level): the limits must keep the tail inside the budget
(Phase 14's research validation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.backtest.events import AccountState, Fill
from xq.backtest.metrics import path_max_drawdowns
from xq.core.config import GateCheck, GatesConfig
from xq.core.time import to_ns, trading_day, trading_day_bounds
from xq.risk.engine import RiskEngine
from xq.risk.state import MarketState, RiskStateTracker
from xq.signals.schema import TradeIntent
from xq.validation.sharpe import politis_white_block_length, stationary_bootstrap

FloatArray = npt.NDArray[np.float64]
#: Columns of `TradeOutcomes.frame`, one row per closed trade in entry order.
OUTCOME_COLUMNS = (
    "entry_time",
    "exit_time",
    "direction",
    "entry_price",
    "spread",
    "sigma_daily",
    "r_multiple",
)
_HALT = "drawdown halt"


@dataclass(frozen=True)
class TradeOutcomes:
    """Closed trades in risk units and their calendar (module docstring)."""

    frame: pd.DataFrame
    stop_sigmas: float

    def __post_init__(self) -> None:
        missing = [c for c in OUTCOME_COLUMNS if c not in self.frame.columns]
        if missing:
            raise ValueError(f"trade outcomes lack columns {missing}")
        if len(self.frame) == 0:
            raise ValueError("the Monte Carlo needs at least one closed trade")
        values = self.frame[["entry_price", "spread", "sigma_daily", "r_multiple"]]
        if not np.isfinite(values.to_numpy(np.float64)).all():
            raise ValueError("trade outcomes must not have missing values")
        if (self.frame["sigma_daily"] <= 0).any() or (self.frame["entry_price"] <= 0).any():
            raise ValueError("sigma-hat and entry prices must be positive")
        if not set(np.unique(self.frame["direction"])) <= {-1, 1}:
            raise ValueError("direction must be +1 (long) or -1 (short)")
        if self.stop_sigmas <= 0:
            raise ValueError("stop_sigmas must be positive")

    @property
    def r(self) -> FloatArray:
        """The R-multiples in entry order."""
        return self.frame["r_multiple"].to_numpy(np.float64)


def trade_outcomes(trades: pd.DataFrame, *, stop_sigmas: float) -> TradeOutcomes:
    """R-multiples of closed trades (module docstring).

    Args:
        trades: One row per closed trade: ``entry_time`` and ``exit_time`` (tz-aware),
            ``direction`` (+1 long, -1 short), ``entry_price`` (mid), ``spread`` (price units),
            ``sigma_daily`` (the daily sigma-hat known at entry, a fraction of price) and
            ``trade_return`` (net P&L over the notional at entry).
        stop_sigmas: The stop distance in daily sigma-hats that defines one R.
    """
    frame = trades.sort_values("entry_time", kind="stable").reset_index(drop=True)
    r = frame["trade_return"].to_numpy(np.float64) / (
        stop_sigmas * frame["sigma_daily"].to_numpy(np.float64)
    )
    out = frame.loc[:, [c for c in OUTCOME_COLUMNS if c != "r_multiple"]].assign(r_multiple=r)
    return TradeOutcomes(out, stop_sigmas)


@dataclass(frozen=True)
class MonteCarloResult:
    """Paths of equity under the risk rules (module docstring)."""

    n_paths: int
    n_trades: int
    mean_block: float
    ruin_level: float
    halt_level: float
    engine_label: str
    #: Per path: maximum drawdown, final equity over capital, halt fired, ruin, entries taken
    #: and refused.
    paths: pd.DataFrame
    #: Resampled trade indices, one row per path (for audit and tests).
    indices: npt.NDArray[np.int64]

    def drawdown_quantile(self, q: float) -> float:
        """The `q` quantile of the paths' maximum drawdowns."""
        return float(np.quantile(self.paths["max_drawdown"].to_numpy(np.float64), q))

    @property
    def halt_probability(self) -> float:
        """Share of paths on which the drawdown halt fired."""
        return float(self.paths["halted"].mean())

    @property
    def ruin_probability(self) -> float:
        """Share of paths whose equity fell to ``ruin_level`` of the capital."""
        return float(self.paths["ruined"].mean())

    def summary(self, quantiles: tuple[float, ...] = (0.05, 0.5, 0.95, 0.99)) -> pd.DataFrame:
        """Quantiles of the maximum drawdown and final equity, and the probabilities."""
        rows: dict[str, float] = {}
        for q in quantiles:
            rows[f"max_drawdown_q{q:g}"] = self.drawdown_quantile(q)
        for q in quantiles:
            rows[f"final_equity_q{q:g}"] = float(np.quantile(self.paths["final_equity"], q))
        rows["halt_probability"] = self.halt_probability
        rows["ruin_probability"] = self.ruin_probability
        rows["mean_entries_taken"] = float(self.paths["taken"].mean())
        rows["mean_entries_refused"] = float(self.paths["refused"].mean())
        return pd.DataFrame({"value": rows})

    def gate_check(self, gates: GatesConfig) -> GateCheck:
        """R2 ``monte_carlo_drawdown``: the gate's quantile of the maximum drawdown."""
        quantile = gates.r2_validated.monte_carlo_drawdown.quantile
        criterion = gates.criterion("R2", "monte_carlo_drawdown.below")
        return criterion.check(self.drawdown_quantile(quantile))


def monte_carlo(
    outcomes: TradeOutcomes,
    engine: RiskEngine,
    *,
    capital: float,
    n_paths: int,
    seed: int,
    ruin_level: float,
    min_block_trades: int = 1,
) -> MonteCarloResult:
    """Resample the trade outcomes and replay every path through `engine` (module docstring).

    Args:
        outcomes: The strategy's closed trades in risk units (`trade_outcomes`).
        engine: The risk engine with the profile under test (``RiskEngine.from_config``).
        capital: Starting equity (USD).
        n_paths: Resampled paths.
        seed: Seed of the resamples.
        ruin_level: Equity, as a share of the capital, at or below which a path is ruined.
        min_block_trades: Lower bound on the Politis-White mean block length (trades).

    Raises:
        ValueError: for a non-positive capital, no paths or a ruin level outside (0, 1).
    """
    if capital <= 0 or n_paths < 1:
        raise ValueError("capital must be positive and n_paths at least 1")
    if not 0 < ruin_level < 1:
        raise ValueError("ruin_level must lie in (0, 1)")
    r = outcomes.r
    n = len(r)
    block = max(float(min_block_trades), politis_white_block_length(r) if n > 3 else 1.0)
    indices = stationary_bootstrap(n, n_boot=n_paths, mean_block=block, seed=seed)
    calendar = _Calendar.of(outcomes)
    rows = [_replay(calendar, r[row], engine, capital, ruin_level) for row in indices]
    return MonteCarloResult(
        n_paths=n_paths,
        n_trades=n,
        mean_block=block,
        ruin_level=ruin_level,
        halt_level=engine.config.limits.max_drawdown,
        engine_label=engine.label,
        paths=pd.DataFrame(rows),
        indices=indices,
    )


@dataclass(frozen=True)
class _Calendar:
    """The trades' fixed calendar as plain arrays (fast to replay)."""

    entry_ns: npt.NDArray[np.int64]
    exit_ns: npt.NDArray[np.int64]
    entry_at: tuple[datetime, ...]
    #: The last instant of the previous trade's trading day when trade k starts a new day, else
    #: -1 (the tracker then closes that day before the entry).
    close_before: npt.NDArray[np.int64]
    direction: npt.NDArray[np.int64]
    price: FloatArray
    spread: FloatArray
    sigma: FloatArray
    stop_sigmas: float

    @classmethod
    def of(cls, outcomes: TradeOutcomes) -> _Calendar:
        frame = outcomes.frame
        entry = pd.DatetimeIndex(frame["entry_time"])
        exit_ = pd.DatetimeIndex(frame["exit_time"])
        if entry.tz is None or exit_.tz is None:
            raise ValueError("trade times must be tz-aware")
        entry_ns = entry.tz_convert("UTC").as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
        exit_ns = exit_.tz_convert("UTC").as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
        if (exit_ns < entry_ns).any():
            raise ValueError("a trade exits before it enters")
        days = [trading_day(t) for t in entry]
        close_before = np.full(len(days), -1, dtype=np.int64)
        for k in range(1, len(days)):
            if days[k] != days[k - 1]:
                close_before[k] = to_ns(trading_day_bounds(days[k - 1])[1]) - 1
        return cls(
            entry_ns=entry_ns,
            exit_ns=exit_ns,
            entry_at=tuple(t.to_pydatetime() for t in entry.tz_convert("UTC")),
            close_before=close_before,
            direction=frame["direction"].to_numpy(np.int64),
            price=frame["entry_price"].to_numpy(np.float64),
            spread=frame["spread"].to_numpy(np.float64),
            sigma=frame["sigma_daily"].to_numpy(np.float64),
            stop_sigmas=outcomes.stop_sigmas,
        )


def _replay(
    calendar: _Calendar,
    r: FloatArray,
    engine: RiskEngine,
    capital: float,
    ruin_level: float,
) -> dict[str, float | bool | int]:
    """One path through the risk engine (module docstring, "Replay")."""
    contract = float(engine.instrument.contract_size)
    exposure = engine.config.limits.max_notional
    tracker = RiskStateTracker(capital, contract)
    equity = capital
    marks = [capital]
    halted = False
    taken = refused = 0
    for k in range(len(r)):
        entry, exit_ = int(calendar.entry_ns[k]), int(calendar.exit_ns[k])
        price, half = float(calendar.price[k]), float(calendar.spread[k]) / 2
        sigma, sign = float(calendar.sigma[k]), int(calendar.direction[k])
        if calendar.close_before[k] >= 0:
            tracker.close_day(_account(int(calendar.close_before[k]), capital, equity, price))
        tracker.observe(_account(entry, capital, equity, price))
        side = price + sign * half  # the ask for a long entry, the bid for a short one
        risk_distance = calendar.stop_sigmas * sigma * price
        intent = TradeIntent(
            direction="long" if sign > 0 else "short",
            exposure=exposure,
            stop=side - sign * risk_distance,
            intent_id=f"MC{k}",
            created_at=calendar.entry_at[k],
        )
        market = MarketState(entry, price - half, price + half, entry, sigma_daily=sigma)
        decision = engine.evaluate(intent, tracker.state(), market)
        lots = decision.target_lots or 0.0
        if not decision.approved or lots == 0:
            refused += 1
            halted = halted or any(reason.startswith(_HALT) for reason in decision.reasons)
            continue
        taken += 1
        move = float(r[k]) * risk_distance  # the price move that realizes R
        exit_price = side + sign * move
        tracker.on_fill(_fill(f"MC{k}e", entry, "entry", lots, side))
        pnl = lots * contract * (exit_price - side)
        equity += pnl
        tracker.on_fill(_fill(f"MC{k}x", exit_, "exit", -lots, exit_price))
        tracker.observe(_account(exit_, capital, equity, exit_price))
        marks.append(equity)
    worst = tracker.state().worst_drawdown if taken else 0.0
    path = np.asarray(marks, dtype=np.float64)
    return {
        "max_drawdown": float(path_max_drawdowns(path, capital)[0]),
        "final_equity": equity / capital,
        "halted": halted or worst >= engine.config.limits.max_drawdown,
        "ruined": bool(path.min() <= ruin_level * capital),
        "taken": taken,
        "refused": refused,
    }


def _account(ts: int, capital: float, equity: float, mark: float) -> AccountState:
    return AccountState(
        ts=ts,
        capital=capital,
        cash=equity,
        unrealized=0.0,
        equity=equity,
        position_lots=0.0,
        margin_used=0.0,
        mark=mark,
    )


def _fill(fill_id: str, ts: int, role: str, lots: float, price: float) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=fill_id,
        decision_id=fill_id,
        intent_id=fill_id,
        ts=ts,
        decided_at=ts,
        role=role,
        order_type="market",
        lots=lots,
        price=price,
        bid=price,
        ask=price,
        mid=price,
        slippage_bps=0.0,
        spread_cost=0.0,
        slippage_cost=0.0,
        commission=0.0,
        position_after=lots if role == "entry" else 0.0,
    )
