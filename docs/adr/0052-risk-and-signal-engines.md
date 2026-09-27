# ADR 0052 — Risk and signal engines (Sprint 12 A)

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** Claude, within the plan (section 6, Phases 14 and 15) and the owner's Sprint 12 A
  instructions (ADR 0051); open points are flagged for the owner's review
- **Tasks:** RISK-001 … RISK-006, SIGNAL-001 … SIGNAL-005

## RISK-001 — risk state

1. **Derived from the account only.** Equity, peak equity, the drawdown `(peak − equity) / peak`
   and the worst drawdown since the start or the last manual reset; the trading day (17:00 New
   York roll), its starting equity (the previous day's closing equity, else the first
   observation) and P&L; the position, its notional and margin; consecutive losing round trips
   (flat to flat or to a flip; realized price P&L net of commissions — financing is not included)
   and when the last one closed; entries filled in the trading day.
2. **Observed at decisions and day ends.** Equity is observed at every risk decision and at every
   trading day's end, not at every quote, so peak equity and drawdown are those of the observed
   equity. The engine records each observation as an `account` row in the decision ledger, next to
   the `fill` rows.
3. **Reconstructable from the ledger.** `rebuild_risk_state` replays the ledger's `account` and
   `fill` rows through the same tracker; a test shows the rebuilt state equals the live state at
   every decision of engine runs (the plan's "rebuild equals live state").
4. **Market state.** `MarketState` holds what the engine knows about the market at a decision: the
   latest quote and its age, the daily sigma-hat, a reference spread, the sessions in force and
   the kill switch's reason. The engine assembles it, so the risk engine stays pure.

## RISK-002 — sizing, and the risk profile

1. **Risk profiles** live in `config/risk/<profile>.yaml` (the plan's `config/risk/*.yaml`),
   chosen by `backtest.risk_profile` (`default`), and carry a `version` recorded on every decision
   with a hash of the profile. The owner's plan defaults are kept: 0.5 % of equity risked per
   trade and new exposure halted at a 15 % drawdown. The other values are provisional choices of
   this ADR (see the profile's comments): throttle from 5 % to 15 % drawdown, probability scaling
   from 0.5 to 0.6, 20 lots, 3 × equity notional, 50 % margin use, a 3 % daily loss, a 4-hour
   cooldown after 5 losing round trips, 12 entries a day, stops between 3 spreads and 5 daily
   sigmas, a 120 s stale-quote breaker and a 5 × median-spread breaker.
2. **Sizing** is fixed fractional (the risk budget over the stop distance) or volatility
   targeting, then capped by the strategy's requested exposure — a strategy can ask for less,
   never more, so models still never size — scaled by the calibrated win probability and by the
   drawdown throttle, and rounded down to the lot step within the instrument's lot range. Every
   step can only shrink the size; property tests pin the caps.

## RISK-003 — limits and halts

1. **Halts refuse new exposure only.** An order that opens, increases or flips a position is
   refused while a halt is in force; an order that only reduces a position is never blocked by a
   halt (RISK-005 applies this). Each halt triggers at its threshold (`>=`): the drawdown halt on
   the *worst* drawdown since the start or the last manual reset (sticky: a recovery does not lift
   it, only `RiskStateTracker.reset_halt` does), the daily loss halt on the trading day's loss as
   a share of its starting equity (lifted at the next trading day), the cooldown for
   `cooldown_minutes` after `max_consecutive_losses` losing round trips in a row (lifted exactly
   at the end, or by a winning round trip), and the entries-per-day halt.
2. **Caps bound the target, not the order.** The target position is cut to the smallest of the
   caps — lots, notional (plus correlated exposure from other instruments through the
   `CorrelatedExposure` hook, none with one instrument), margin use and, while a named session is
   in force, the session's exposure — and rounded down to the lot step. Caps use the decision's
   reference price (the side's quote) and equity at the decision; equity at or below zero allows
   no exposure. The decision records which caps bound it.
3. **Floating point.** Sizes are rounded to the lot step from their value at 12 decimals, as in
   RISK-002, so a cap can be exceeded by at most 5e-13 lots through representation error; the
   property test allows a relative 1e-9.

## RISK-004 — stop policy

1. **Every long or short intent carries a price stop**, including one that only reduces a
   position on the same side: a new order cancels the earlier intent's bracket when it reaches
   the broker, and its own stop and target become the bracket on the whole resulting position, so
   an order without a stop would leave the rest of the position unprotected. A `flat` intent needs
   none. Time stops are allowed *in addition*, never instead: fixed-fractional sizing
   needs the distance to a price stop.
2. **Bounds** (the plan's `[k_min·spread, k_max·σ̂]`), measured from the entry reference — the
   side's quote for a market entry, the order's price for a limit or stop entry: a stop closer
   than `min_spread_multiple` × the current spread, and at least one tick, is **widened** outward
   to the tick (it becomes the decision's `adjusted_stop`, and sizing uses the widened distance,
   so the risk budget still holds); a stop farther than `max_sigma_multiple` × daily σ̂ × price is
   **refused**, not tightened — tightening would change the strategy's exit, and the plan's
   refusal is the conservative reading. Without a σ̂ at the decision no stop can be bounded and
   the entry is refused.
3. **Sanity of the bracket.** A target must be on the winning side of the entry reference (a
   target already through the market would close the position at once), and a time stop must be
   after the decision time.
