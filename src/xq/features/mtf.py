"""Multi-timeframe context (FEAT-008).

A feature set may compute any registered feature on the bars of one of the dataset's context
timeframes (``timeframe: 1h``, ``4h``, ``1d`` in ``config/features.yaml``; the base 15m bars are
the default). The feature runs on that timeframe's complete bars exactly as on the base bars, and
its values are joined onto the base decision times **on availability**: each decision reads the
value of the latest context bar whose ``available_at`` is at or before it (`asof_join`), never the
bar that contains the decision, never by bar start. A 1h bar starting at 10:00 is therefore read
from 11:00 plus its publication latency on, and a 1d bar only after the trading day closes.

Columns are named ``mtf_<timeframe>_<column>``, with one provenance column
``mtf_<timeframe>_available_at`` per timeframe (the availability of the context bar each decision
read), which the leakage harness audits: it may never be later than the decision.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from xq.core.types import Timeframe
from xq.datasets.asof import PROVENANCE_SUFFIX, asof_join
from xq.datasets.base_features import DECISION_TIME
from xq.features.base import AVAILABLE_AT

MTF_PREFIX = "mtf_"


def context_prefix(timeframe: Timeframe) -> str:
    """``mtf_<timeframe>_``: the column prefix of features computed on `timeframe` bars."""
    return f"{MTF_PREFIX}{timeframe.value}_"


def join_on_availability(
    decisions: pd.DatetimeIndex, computed: pd.DataFrame, prefix: str
) -> pd.DataFrame:
    """`computed` (indexed by its bars' availability) at each decision: the row of the latest bar
    available by then, found by `asof_join` on ``available_at``, with the provenance column
    ``<prefix>available_at`` (missing values before the first available bar)."""
    rows = pd.DataFrame({"row": np.arange(len(computed)), AVAILABLE_AT: computed.index})
    joined = asof_join(
        pd.DataFrame({DECISION_TIME: decisions}),
        rows,
        on_right=AVAILABLE_AT,
        prefix=prefix,
        columns=["row"],
    )
    matched = joined[f"{prefix}row"].to_numpy(dtype=np.float64)
    known = ~np.isnan(matched)
    if len(computed):
        values = computed.to_numpy(np.float64)[np.where(known, matched, 0).astype(np.int64)]
        values[~known] = np.nan
    else:  # no bar of the timeframe available yet
        values = np.full((len(decisions), computed.shape[1]), np.nan)
    out = pd.DataFrame(values, index=decisions, columns=[f"{prefix}{c}" for c in computed.columns])
    out[f"{prefix}{PROVENANCE_SUFFIX}"] = joined[f"{prefix}{PROVENANCE_SUFFIX}"].to_numpy()
    return out
