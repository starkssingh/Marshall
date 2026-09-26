# ADR 0008 — SPIKE measures whole-quote moves, scaled by elapsed time

- **Status:** accepted; supersedes the `SPIKE` definition in ADR 0006 §3
- **Date:** 2026-09-26
- **Tasks:** DATA-007 (found while building DATA-009 test data)

## Context

ADR 0006 defined `SPIKE` on mid returns scaled by a per-tick robust scale. A synthetic week with
realistic rollover behaviour showed two false-positive sources:

1. **One-sided spread changes.** When a feed widens only the ask around the 17:00 rollover, the mid
   jumps and then "reverts" as the spread varies. 22 ticks in one synthetic week were flagged, none
   of them a price spike.
2. **Moves across pauses.** A return across the daily break or any quiet spell is naturally much
   larger than a tick-to-tick return, so a normal reopen move followed by a partial give-back
   looked like an 8-sigma spike.

## Decision

1. The candidate return is the **common move of bid and ask**: when both log returns have the same
   sign, the one with the smaller magnitude; otherwise zero. A spike must move the whole quote.
   One-sided blow-outs remain the job of `SPREAD_OUTLIER` and `CROSSED`.
2. The robust scale (1.4826 × trailing median |mid log return|, floored at `min_scale_bps`) is
   multiplied by `sqrt(max(1, elapsed / trailing median tick spacing))`, the random-walk scaling of
   a return over the elapsed time. The spacing median uses the same `window_ticks`/`min_periods`.
3. Confirmation is unchanged: within `reversal_ticks` later ticks the mid must come back by at
   least `reversal_fraction` of the jump. `SPIKE` remains non-causal and may not exclude ticks from
   bars.
4. `CLEAN_CODE_VERSION` becomes 2, so every rules version changes and clean stores built with the
   old definition are kept separately rather than overwritten.

## Consequences

- Tests: an injected two-sided jump is still flagged exactly; one-sided rollover widening and a
  gap move that half-reverts are not flagged.
- A genuine bad print that moves only one side is no longer a `SPIKE`; it is caught by
  `SPREAD_OUTLIER` (bid spikes down, ask spikes up) or `CROSSED` (bid above ask).
- `SPREAD_OUTLIER` still fires on rollover widening, because its reference is the trailing median
  spread. That is expected, documented in ADR 0006, and to be judged on real broker data in DQ-008.
