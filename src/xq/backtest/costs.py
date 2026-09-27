"""Cost model: spread, commission, slippage, financing and latency (BT-001).

A venue's costs live in ``config/costs/<name>.yaml`` (``backtest.cost_model`` picks one). The
default ``placeholder`` model is PROVISIONAL: the broker is not yet named, so its values are
conservative assumptions, not broker terms (ADR 0029).

- **Spread** is paid through fills at the correct side of real quotes (buy at the ask, sell at the
  bid). Where only mid prices exist, `fallback_spread` gives the hour-of-week spread percentile of
  the source's clean ticks (DATA-009).
- **Commission** per fill and side: ``|lots| * per_lot_per_side_usd`` plus
  ``per_notional_per_side_bps`` of the notional.
- **Slippage** in basis points of the fill price: ``(fixed_bps + sigma_multiple * sigma_1m_bps)``
  times the largest configured multiplier whose session or event window contains the fill time
  (the rollover window, the US data release window, ...), 1 when none does.
- **Financing** at every daily rollover (the instrument's rollover time, 17:00 New York, on each
  open trading day) on the notional held over it: ``notional * rate / 100 / day_count``, with the
  long or short rate by the position's sign, three times on the configured weekday.
- **Latency**: orders arrive ``latency_ms`` of market time after the decision; a fill more than
  ``max_fill_delay_s`` after that is missed.

Amounts are USD (the quote currency of XAUUSD); a positive cost reduces P&L. A provisional model
charges financing on both sides (ADR 0032), and every net result computed with it carries
`SCREENING_LABEL` (`CostModel.result_label`): reports print it with each net figure.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import numpy as np
import numpy.typing as npt
import pandas as pd
from sqlalchemy import Engine

from xq.core.config import WEEKDAYS, AppConfig, CostModelConfig, InstrumentSpec, SessionsConfig
from xq.core.errors import ConfigError
from xq.core.time import local_time_to_utc, trading_day, trading_day_bounds
from xq.data.calendar import MarketCalendar
from xq.data.spreads import NoSpreadDataError, hour_of_week, latest_spread_stats
from xq.datasets.calendar_columns import calendar_columns

FloatArray = npt.NDArray[np.float64]
_BPS = 1e-4
_MINUTE_NS = 60_000_000_000
#: The mark of every net result computed with a provisional cost model (ADR 0032).
SCREENING_LABEL = "screening, placeholder costs"


class CostModel:
    """Costs of trading one instrument at one venue (see the module docstring)."""

    def __init__(
        self,
        config: CostModelConfig,
        instrument: InstrumentSpec,
        sessions: SessionsConfig,
        spread_stats: pd.DataFrame | None = None,
    ) -> None:
        self.config = config
        self.instrument = instrument
        self.sessions = sessions
        self.spread_stats = spread_stats
        self._multiplier_columns = {
            key: _window_column(key, sessions) for key in config.slippage.multipliers
        }
        # every calendar boundary on a whole minute: the multiplier is constant within a minute
        self._minute_exact = all(
            t.second == 0 and t.microsecond == 0 for t in _times(sessions.model_dump())
        )
        self._day_multipliers: dict[int, FloatArray] = {}

    @classmethod
    def from_config(
        cls,
        cfg: AppConfig,
        instrument_id: str,
        *,
        engine: Engine | None = None,
        source_id: str | None = None,
        name: str | None = None,
    ) -> CostModel:
        """The configured cost model, with the source's spread statistics if they exist."""
        stats = None
        if engine is not None and source_id is not None:
            try:
                stats = latest_spread_stats(engine, source_id)
            except NoSpreadDataError:
                stats = None
        return cls(
            cfg.cost_model_config(name),
            cfg.instrument(instrument_id),
            cfg.sessions_config(),
            stats,
        )

    @property
    def result_label(self) -> str:
        """How net results computed with this model must be labelled in reports."""
        if self.config.provisional:
            return SCREENING_LABEL
        return f"net of {self.config.venue} costs"

    @property
    def latency(self) -> pd.Timedelta:
        """Decision to order arrival, in market time."""
        return pd.Timedelta(milliseconds=self.config.latency_ms)

    @property
    def max_fill_delay(self) -> pd.Timedelta:
        """How long after the intended time a fill may still happen."""
        return pd.Timedelta(seconds=self.config.max_fill_delay_s)

    def commission_usd(self, lots: npt.ArrayLike, price: npt.ArrayLike) -> FloatArray:
        """Commission of fills of `lots` (either sign) at `price`."""
        size = np.abs(np.asarray(lots, dtype=np.float64))
        notional = size * float(self.instrument.contract_size) * np.asarray(price, np.float64)
        commission = self.config.commission
        charge: FloatArray = (
            size * commission.per_lot_per_side_usd
            + notional * commission.per_notional_per_side_bps * _BPS
        )
        return charge

    def slippage_bps(self, fill_times: pd.DatetimeIndex, sigma_1m_bps: npt.ArrayLike) -> FloatArray:
        """Slippage of fills at `fill_times`, given sigma-hat of 1-minute returns in bps."""
        slippage = self.config.slippage
        sigma = np.asarray(sigma_1m_bps, dtype=np.float64)
        if np.any(sigma < 0):
            raise ValueError("sigma must not be negative")
        base = slippage.fixed_bps + slippage.sigma_multiple * sigma
        result: FloatArray = base * self._multipliers(pd.DatetimeIndex(fill_times))
        return result

    def _multipliers(self, fill_times: pd.DatetimeIndex) -> FloatArray:
        """The largest slippage multiplier applying at each time (1 where none does)."""
        multiplier: FloatArray = np.ones(len(fill_times))
        if self._multiplier_columns and len(fill_times):
            columns = calendar_columns(fill_times, self.sessions)
            for key, column in self._multiplier_columns.items():
                inside = columns[column].to_numpy(dtype=bool)
                value = self.config.slippage.multipliers[key]
                multiplier = np.where(inside, np.maximum(multiplier, value), multiplier)
        return multiplier

    def slippage_bps_at(self, ts: int, sigma_1m_bps: float) -> float:
        """`slippage_bps` of one fill at `ts` (UTC nanoseconds), for the event tier.

        The same formula; the multiplier is looked up in a per-minute table built once per
        trading day, which is exact because every configured session, anchor and window boundary
        falls on a whole minute (otherwise every call computes it directly).
        """
        if sigma_1m_bps < 0:
            raise ValueError("sigma must not be negative")
        slippage = self.config.slippage
        base = slippage.fixed_bps + slippage.sigma_multiple * sigma_1m_bps
        if not self._multiplier_columns:
            return float(base)
        if not self._minute_exact:
            times = pd.DatetimeIndex([pd.Timestamp(ts, tz="UTC")])
            return float(self.slippage_bps(times, [sigma_1m_bps])[0])
        start, end = trading_day_bounds(trading_day(pd.Timestamp(ts, tz="UTC")))
        table = self._day_multipliers.get(start.value)
        if table is None:  # 23, 24 or 25 hours of minutes (DST changes)
            grid = pd.date_range(start, end, freq="1min", inclusive="left")
            table = self._multipliers(grid)
            self._day_multipliers[start.value] = table
        return float(base * table[(ts - start.value) // _MINUTE_NS])

    def fallback_spread(self, times: pd.DatetimeIndex) -> FloatArray:
        """Hour-of-week spread percentile (price units) at `times`, for quotes without bid/ask.

        Raises:
            NoSpreadDataError: without spread statistics, or for an hour with none.
        """
        if self.spread_stats is None:
            raise NoSpreadDataError("no spread statistics: run `xq spread-stats` for the source")
        quantile = self.config.spread.fallback_quantile
        table = self.spread_stats.set_index("hour_of_week")[quantile]
        stamps = pd.DatetimeIndex(times).as_unit("ns").to_numpy("datetime64[ns]").view(np.int64)
        hours = hour_of_week(stamps)
        values = table.reindex(hours).to_numpy(dtype=np.float64)
        if np.isnan(values).any():
            missing = sorted({int(h) for h in hours[np.isnan(values)]})
            raise NoSpreadDataError(f"no spread statistics for hours of week {missing}")
        return values

    def rollovers(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        """Financing multipliers (1, or 3 on the triple weekday) at rollovers in ``[start, end)``.

        One rollover per open trading day, at the instrument's rollover time on that day.
        """
        first = trading_day(start) - timedelta(days=1)
        last = trading_day(end) + timedelta(days=1)
        calendar = MarketCalendar.for_range(self.sessions, first, last)
        rollover = self.instrument.rollover
        triple = WEEKDAYS.index(self.config.financing.triple_weekday)
        instants, multipliers = [], []
        for offset in range((last - first).days + 1):
            day: date = first + timedelta(days=offset)
            if not calendar.status(day).is_open:
                continue
            instant = local_time_to_utc(day, rollover.time, rollover.tz)
            if start <= instant < end:
                instants.append(instant)
                multipliers.append(3 if day.weekday() == triple else 1)
        return pd.Series(
            multipliers, index=pd.DatetimeIndex(instants, tz="UTC", name="rollover"), dtype="int64"
        )

    def financing_usd(
        self, lots: npt.ArrayLike, price: npt.ArrayLike, multiplier: npt.ArrayLike
    ) -> FloatArray:
        """Financing of positions of `lots` (signed) held over rollovers at `price`."""
        held = np.asarray(lots, dtype=np.float64)
        financing = self.config.financing
        rate = np.where(held > 0, financing.long_rate_annual_pct, financing.short_rate_annual_pct)
        notional = (
            np.abs(held) * float(self.instrument.contract_size) * np.asarray(price, np.float64)
        )
        charge: FloatArray = (
            notional * rate / 100.0 / financing.day_count * np.asarray(multiplier, np.float64)
        )
        return charge


def _window_column(key: str, sessions: SessionsConfig) -> str:
    """The calendar column a slippage multiplier key refers to."""
    if key.endswith("_window") and key.removesuffix("_window") in sessions.event_windows:
        return f"in_{key}"
    if key in sessions.sessions or key in sessions.overlaps:
        return f"in_{key}"
    known = [
        *sessions.sessions,
        *sessions.overlaps,
        *(f"{w}_window" for w in sessions.event_windows),
    ]
    raise ConfigError(f"slippage multiplier {key!r} is not a session or event window: {known}")


def _times(value: object) -> list[time]:
    """Every clock time inside a dumped configuration (nested dicts and lists)."""
    if isinstance(value, time):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _times(v)]
    if isinstance(value, list | tuple):
        return [t for v in value for t in _times(v)]
    return []
