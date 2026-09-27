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
