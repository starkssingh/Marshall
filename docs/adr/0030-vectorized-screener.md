# ADR 0030 — Vectorized screener

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** BT-002 (on BT-001; used by BASE-002, BASE-005)

## Context

BT-002: positions decided at t, filled at the next quote on the correct side plus slippage; P&L in
USD and returns; costs charged on turnover; financing on held positions; aggregation to trading
days. Acceptance: hand-computed P&L and no same-bar fills. Failure conditions (Phase 13): fills at
the signal bar's close, mid-price fills, ignored financing.

## Decision

1. **Positions are target exposures** relative to `backtest.capital_usd` (+1 = long notional equal
   to capital). A change is sized in lots at the mid of its fill quote. An unchanged target keeps
   its lots, so there are no rebalancing trades and turnover is driven by decisions only. The
   screener is a research tool that models no orders. The event-driven tier (BT-004 onward) routes
   orders through the risk engine, which does the sizing (CLAUDE.md: models never size positions).
2. **Fills** use the first quote at or after `latency` of market time past the decision (ADR 0026),
   at the ask for buys and the bid for sells, moved by slippage. A decision taken while the market
   is closed fills after the reopen. A fill more than `max_fill_delay_s` late is *missed*: the
   position stays, and the next decision starts from the position actually held. Missed
   decisions are reported.
3. **Accounting.** Each trading day's equity is capital, plus the cash of fills, minus commission
   and financing, plus the position marked at the mid of the last quote before the day end.
   `net_pnl` is the equity change. `gross_pnl` is the same with fills at mid and no costs, so
   `net = gross − spread − slippage − commission − financing` holds exactly (tested). A rollover
   happens at the day end and belongs to the day it closes; a fill at that instant belongs to the
   next day. Daily returns are `net_pnl / capital` (no compounding).
4. **Trades** are holding episodes, from leaving flat or flipping side until returning to flat or
   flipping. A flip splits its fill between the two episodes by lots. An episode's P&L includes
   its commission and the financing of rollovers it held over. An episode still open at the end
   is marked at the last mid and flagged `open`.

## Consequences

- Results depend on the placeholder cost model (ADR 0029) until the broker's terms are known.
- Without lot rounding or margin, the screener can hold positions a real account could not. The
  event engine and risk engine add those constraints, and BT-009 reconciles the two tiers.
