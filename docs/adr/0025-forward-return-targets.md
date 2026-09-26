# ADR 0025 — Execution-aware forward-return targets

- **Status:** accepted; latency, fill delay and sigma-hat span are provisional
- **Date:** 2026-09-26
- **Tasks:** TGT-002 (on the framework of ADR 0024)

## Context

TGT-002: log return from the execution price at t + latency (next ask for longs, next bid for
shorts; mid variant for symmetric research) to t + h; default horizons 15m, 1h, 4h, 1d, restricted
to those admitted by EDA-006; a vol-normalized variant r / sigma-hat_t. Failure condition: targets
using the signal bar close as entry. Binding convention 4: execution happens at the first quote at
or after t + latency, on the correct side.

## Decision

1. **Fills.** Entry at the first usable clean tick at or after `t + latency`; exit at the first at
   or after `t + h + latency`. Long: ask in, bid out. Short: bid in, ask out. Mid: mid both
   ways (research only; never a trade price). `label_start` / `label_end` are the two fill times.
2. **No stale fills.** If a fill would come more than `max_fill_delay_s` (300 s) after its intended
   time — daily break, weekend, holiday, an excluded day, the end of the data — the target has no
   value. Horizons are calendar durations, so a 1-day label from a Friday has no value rather than
   a weekend-gap fill.
3. **Latency.** `execution_latency_ms` = 1000 (decision to order arrival), a provisional
   assumption, not a broker fact; BT-001's cost model and paper-trading data will replace it via a
   new target set version.
4. **Vol-normalized variant.** `<name>_vol` = return / (sigma_t * sqrt(h in minutes)), where
   sigma_t is the interim sigma-hat known at t: an EWMA (span 96 base bars) of squared 1-bar log
   returns of the dataset's base close, expressed per square-root minute. VOL-006 (Sprint 6) will
   supply a selected forecaster in a new version; `scale` records the value used.
5. **Horizons.** `fwd_returns.v1` uses all four plan defaults for long, short and mid, with and
   without normalization (24 targets). EDA-006 (Sprint 5) admits horizons; a set with only
   admitted horizons will be a new version.
6. **Leakage.** Every configured target passes the bound checks (nothing after `label_end`,
   nothing before t, sigma-hat only at t) and the sigma-hat function passes the feature harness.
   The bound checks found an indexing bug (a missing exit quote read past the end of the quote
   array) before any dataset was built; it is fixed.

## Consequences

- Target returns already pay the spread on both legs; costs beyond the spread (commission,
  slippage, financing) belong to the cost model (BT-001), not to the target.
- `experiments/configs/ds_base.yaml` now names `fwd_returns.v1`.
