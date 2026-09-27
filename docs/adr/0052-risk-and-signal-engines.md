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
