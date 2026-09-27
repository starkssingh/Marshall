# ADR 0029 — Cost model (provisional placeholder)

- **Status:** accepted; every cost value is PROVISIONAL until the broker is named
- **Date:** 2026-09-27
- **Tasks:** BT-001 (used by BT-002, BASE-002, BASE-005)

## Context

BT-001: spread from data or the hour-of-week fallback; commission per lot or per notional by
venue; slippage = fixed + k·σ̂, scaled by session and news multipliers; financing per night with
long and short rates and a triple-rollover day; latency. Acceptance: golden cost cases, including
the triple rollover. The broker is not named (ADR 0004, ADR 0013), and its terms must not be
guessed as facts.

## Decision

1. **One file per venue** in `config/costs/<name>.yaml` (a new config fragment directory), chosen
   by `backtest.cost_model` in `config/base.yaml`. A model declares `provisional: true|false`.
2. **Placeholder model** (`config/costs/placeholder.yaml`, provisional), with values chosen to be
   conservative for a retail XAUUSD CFD rather than to match any broker:
   - commission 3.5 USD per lot per side;
   - slippage 0.5 bp + 0.1 × σ̂ of 1-minute returns (bp), ×3 in the 16:45–18:15 New York
     rollover window and ×2 in the US data release window (the largest multiplier applies);
   - financing 6 %/year on longs and 2 %/year on shorts (both a cost), actual/360, triple on
     Wednesday;
   - latency 1 s of market time, fills more than 300 s late are missed;
   - spread fallback at the p90 of the hour-of-week spread statistics.
3. **Spread** is normally paid through fills at the correct side of real quotes. The hour-of-week
   percentile is only a fallback where no bid/ask exists, and is reported as the spread cost.
4. **Slippage multipliers** are keyed by the session, overlap and event-window names of
   `config/sessions.yaml`, and evaluated at the fill time with the dataset calendar columns
   (DS-007), so costs and features use one calendar.
5. **Financing** is charged at the instrument's rollover time (17:00 New York) on every open
   trading day of the calendar, on the notional held over that instant, with the triple weekday
   covering the weekend. Closed holidays have no rollover. Whether a broker charges financing
   over a holiday is a broker term to confirm.
6. **Units.** Slippage is in basis points (convention 7: never fixed dollars). Commission and
   financing are USD amounts, because that is how venues charge them.

## Consequences

- Every backtest result is a screening result until the placeholder is replaced by the broker's
  terms (an owner decision in `docs/STATUS.md`) and checked against paper-trading slippage.
- Robustness work (Phase 16) stresses these values (1.5× spread, 2× slippage) rather than
  trusting them.
