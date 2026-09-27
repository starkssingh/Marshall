# ADR 0051 — Sprint order while real data is pending: Sprint 12's data-independent part, then Sprint 9

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, at the Sprint 11 review; extends ADR 0048
- **Tasks:** Sprint 12 (RISK-001 … RISK-006, SIGNAL-001 … SIGNAL-005); Sprint 9 (VAL-003, VAL-004,
  VAL-006, ROB-001, ROB-002, ROB-003, ROB-006, ROB-007, EXP-006)

## Context

ADR 0048 ran Sprint 11 ahead of Sprints 7–10 and left Sprint 12's scope open because several of
its tasks depend on the postponed sprints: SIGNAL-003's regime filter and ROB-006 on REG-007
(Sprint 8), ROB-004 on ROB-003 (Sprint 9), ROB-005 on ROB-001 (Sprint 9), ROB-008 on ROB-001 …
ROB-007. Real broker data is still pending (C-8).

## Decision

**A. Now: Sprint 12's data-independent part.**
- RISK-001 … RISK-006: the risk state (reconstructable from the ledger), sizing, limits and halts,
  the stop policy, `RiskEngine.evaluate` with the `OrderIntent` construction rule, the kill
  switch and data-health breakers.
- SIGNAL-001, SIGNAL-002, SIGNAL-004, SIGNAL-005: the schemas with JSON Schema export, the EV
  calculator, the orchestration with audit records, and the integration with the event
  backtester.
- SIGNAL-003 with the **regime filter as an interface only**: it accepts a `RegimeState` and
  ships a pass-through implementation, clearly marked, because no regime model exists yet
  (REG-007, Sprint 8). The session, blackout, volatility-band and spread filters are real.
- The Sprint 11 placeholder risk approver is **replaced by the real `RiskEngine` everywhere**; the
  placeholder module is removed.
- Required tests: sizes never exceed any limit (property tests); halts trigger exactly at their
  thresholds; an architectural test fails if any `OrderIntent` is built without an approved
  `RiskDecision`; the signal engine rejects uncalibrated forecasts; golden EV cases; a
  forecast-to-fill run in the event backtester with a complete audit trail.
- Synthetic data only; the session ends with a pull request and waits for the owner's review.

**B. Next (a later session, after that review): Sprint 9** — VAL-003, VAL-004, VAL-006, ROB-001,
ROB-002, ROB-003, ROB-006, ROB-007, EXP-006 — validated on simulated overfit versus genuine-edge
strategies.

**Deferred with their dependencies:** ROB-004, ROB-005 and ROB-008 (Sprint 12 in the plan) wait for
Sprint 9's ROB tasks; the real regime filter waits for REG-007 (Sprint 8).

## Consequences

- After A, every event-backtest order goes through the real risk engine; results remain
  engineering tests on synthetic data, and costs remain "screening, placeholder costs".
- Sprint 12's plan verification ("a candidate runs forecast → EV → filters → risk → execution with
  a complete audit trail, Monte Carlo and robustness score") is met except for the Monte Carlo and
  robustness score, which arrive with ROB-004 and ROB-008 after Sprint 9.
