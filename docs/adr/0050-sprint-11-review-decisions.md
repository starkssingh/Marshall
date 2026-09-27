# ADR 0050 — Owner decisions at the Sprint 11 review

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, reviewing PR #12 (Sprint 11, the event-driven backtester)
- **Tasks:** BT-005, BT-008, BT-009 (ADR 0049 open points, C-20); TGT-002 (C-21)

The owner's message grouped the approved defaults and the limit-order rule under C-21; in
`docs/STATUS.md` they were part of C-20 and C-21 was the TGT-002 fix. This ADR records every
decision once, whatever its label.

## Decisions

1. **Reconciliation tolerance after sizing (C-20).** The reconciliation keeps reporting the
   lot-rounding sizing effect separately, and the 5 %-of-costs tolerance applies to the equity
   difference *after* removing it. Concretely, a *sized* screen replays the screener's own
   decisions with the event tier's lot sizes wherever the two tiers filled the same decision on the
   same quote (and skips the trades the event tier's rounding made unnecessary); the sizing effect
   is sized − screener, and `within_tolerance` compares the largest daily |event − sized| with 5 %
   of the screener's total costs. Differences from event-only rules (blackouts, risk rejections)
   and their follow-ons stay inside the tolerance check, and the mechanical residual must still
   be below one cent.
2. **Provisional defaults approved.** The 60-minute entry blackout before a weekly close; the
   flat-before-weekend exit optional and off by default (30 minutes before the weekly close when
   switched on); margin 5 % of notional (1:20). They stay provisional until broker terms and paper
   trading, but are no longer open questions.
3. **Limit orders fill only when the price trades through the limit by at least one tick.** A sell
   limit at L fills when a quote's bid is at least L + 1 tick, a buy limit when the ask is at most
   L − 1 tick; a quote that only touches the limit is not a fill. The fill is at L, never better.
   The rule applies to limit entries and to take-profit legs, in tick mode and in bar mode (a
   bar's open or range must go at least one tick through L), and to the bar-mode ambiguity check
   (a bar is ambiguous only when it goes through the target and reaches the stop). The tick is the
   instrument's `tick_size` (0.01 for XAUUSD).
4. **TGT-002 closed-market fills are fixed now (C-21).** No real dataset exists, so nothing is
   lost: forward-return labels take the first quote at or after the intended fill time that lies
   in market hours, as the screener (`aa88b2a`) and the event tier do. The forward-return target
   code version is bumped, which changes target-set definition hashes and dataset ids; the
   affected tests and hashes are updated.

## Consequences

- C-20 and C-21 close with the commits that implement decisions 1, 3 and 4.
- Golden cases whose take profit fills on a quote that trades through by at least a tick are
  unchanged; a touch-only case is added to the tests.
