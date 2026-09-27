# ADR 0040 — EDA-006 after the owner's review: TGT-002 holding periods and admission safeguards

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, reviewing PR #9 (Sprint 5)
- **Tasks:** EDA-006; amends ADR 0037

## Context

The owner reviewed the horizon admission of ADR 0037 and asked for six changes, one commit each
with tests. The first version measured a horizon by the close-to-close returns of bars of that
timeframe, which is not how a target (TGT-002) or a strategy holds a position.

## Decision 1 — Holding periods are TGT-002's

- Candidate horizons are TGT-002 horizon labels (`1m` … `1d`; `1d` is one trading day of 23
  market hours, `market_horizon`), not bar timeframes.
- Decisions are the 1m bars whose availability falls on the `horizons.decision_step` grid (5
  minutes). Each 1m bar contributes one quote, its closing mid and spread, stamped just before the
  bar's end.
- The move of a decision is computed by TGT-002's own `xq.targets.returns.compute` (mid
  variant) on those quotes, with the execution latency and allowed fill delay of the
  `horizons.target_set` target set (`fwd_returns.v1`). So the horizon arithmetic (trading time),
  the closed-market rule (no measurement for a decision taken while the market is closed), the
  fill-delay rule and `crosses_close` are the same code as the targets'.
- Periods overlap (a decision every 5 minutes); that is fine for means, and n is reported, with
  the share of periods that cross a close. Sessions are those of the decision time.
- The fills come only from discovery-window bars, and `run_eda` checks every period's exit
  against the window.

## Decision 2 — Spread at the fills

The spread cost is half the quoted spread at the entry fill plus half the spread at the exit fill,
each over its own mid: the closing spread (`spread_close`) of the 1m bar whose close is the fill.
The bar's mean spread over the holding period averaged in spread spikes (the rollover) and quiet
stretches that the trade never meets.
