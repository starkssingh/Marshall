# ADR 0049 — The event-driven backtester (Sprint 11)

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** Claude, within the plan (section 6, Phase 13) and the owner's Sprint 11
  instructions (ADR 0048); open points are flagged for the owner's review of Sprint 11
- **Tasks:** BT-004, BT-005, BT-006, BT-007, BT-008, BT-009, BT-010

## Context

The plan asks for a two-tier backtester: the vectorized screener (BT-002, done) for research and
an event-driven reference simulator for candidates, with a reconciliation test between them
(section 2, assumption 8). The event tier must route every order through a risk decision
(`RiskEngine.evaluate`, RISK-005, Sprint 12), which does not exist yet, and is built and tested
on synthetic data only (ADR 0048).

## BT-004 — event core

1. **Events and ordering.** `TickEvent`, `BarEvent`, `SignalEvent`, `OrderEvent`, `FillEvent`,
   `TimerEvent` as the plan lists, plus `ExecutionBarEvent` for execution without ticks (bar
   mode). The queue orders by `(timestamp, rank, sequence)`. The plan says `(timestamp,
   sequence)`; the rank is added so that "at or after" means the same in both tiers whatever the
   insertion order: at one instant, timers (rollover financing, day-end marks, expiries) come
   first, then order arrivals, then quotes, then fills, then signal bars, then intents. An order
   arriving at t can fill on a quote at t (the screener's first quote at or after the intended
   time); a rollover at t charges the position held strictly before t and a day's end marks with
   the last quote strictly before it (as BT-002 does).
2. **Deterministic clock.** `SimulationClock` moves only with processed events and refuses to go
   back. Ids are counters (`I000001`, `D-I000001`, `O-I000001`, ...), never random, so two runs
   of the same inputs produce the same trace, ledger and result.
3. **Decision chain and schemas.** A strategy returns `TradeIntent`s from `on_bar(bar, ctx)`; the
   engine stamps the intent id and the decision time (the bar's `available_at`), so a strategy
   cannot choose its decision time. `TradeIntent`, `RiskDecision` and `OrderIntent` are built now
   in their planned home, `xq.signals.schema`, in the plan's shape (Phase 15) plus the fields the
   engine needs (`exposure`, the requested exposure a fraction of capital; `target_lots` and
   `side` on the decision; `expected_position_lots` on the order). `OrderIntent` can only be
   built from an approved `RiskDecision` and must carry its side, size, stop and target (enforced
   by the type). SIGNAL-001 completes the schema set and exports JSON Schemas in Sprint 12.
4. **Placeholder risk approver.** `xq.risk.placeholder.PassThroughRiskApprover` approves every
   intent without any risk check and sizes it mechanically: sign × exposure × initial capital ÷
   (mid of the latest quote at the decision × contract size), rounded down to the lot step
   within the minimum and maximum lot. Every decision carries `config_version
   "placeholder-pass-through-1"` and the reason "PLACEHOLDER pass-through approver: no risk
   checks"; every result and report carries its label. RISK-005 replaces it in Sprint 12.
5. **Data modes.** Tick mode: the broker executes on quotes; signal bars are built from the same
   quotes by the DATA-008 `build_bars` (bars ending after the data's coverage are dropped). Bar
   mode: the broker executes on one-minute bid/ask bars; each is streamed as its open (at the
   bar's start) and its range (just before its end). The strategy sees only completed signal bars
   at their `available_at`.
6. **Closed market.** A decision taken while the market is closed places no order (ADR 0032); the
   engine refuses it before the risk decision and records the refusal.
7. **Layout.** New modules: `xq/backtest/events.py` and `engine.py` (planned),
   `xq/signals/schema.py` (planned, partial), `xq/risk/placeholder.py` (temporary, removed when
   RISK-005 lands).
