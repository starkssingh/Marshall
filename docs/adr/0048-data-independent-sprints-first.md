# ADR 0048 — While real data is pending, the data-independent engineering sprints run next

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, at the start of Sprint 11 (after merging PR #11, Sprint 6)
- **Tasks:** sprint order (development plan, section 10); Sprint 11 (BT-004 … BT-010); Sprint 12

## Context

The plan's build order after Sprint 6 is Sprint 7 (features and targets), 8 (regimes and
diagnostics), 9 (significance and robustness core) and 10 (ML stages A and B), then 11 (the
event-driven backtester) and 12 (risk and signal engines). No real broker data exists yet (C-8):
the research halves of Sprints 5 and 6 wait for it (C-16, C-18), and Sprints 7–10 are mostly
research or produce their first meaningful output on real data.

The event-driven backtester, on the other hand, depends only on work that is done (BT-001 …
BT-003, DATA-001, DATA-002) and is tested entirely on synthetic quotes with hand-computed answers.
The plan itself notes that BT-004 needs no ML code (section 8, critical path); its placement after
Sprint 10 is a research dependency (nothing worth backtesting until a candidate exists), not a
code dependency.

## Decision

1. While real broker data is pending, the data-independent engineering sprints run next:
   **Sprint 11** (BT-004, BT-005, BT-006, BT-007, BT-008, BT-009, BT-010, in that order) now,
   then **Sprint 12** (risk and signal engines) **after the owner's review of Sprint 11**.
2. Sprint 11 uses **synthetic data only**: golden hand-computed trades and simulated quotes. No
   backtest, reconciliation or report runs on real or pseudo-real data.
3. Until Sprint 12 builds `RiskEngine.evaluate` (RISK-005), the event engine routes every intent
   through a **pass-through placeholder risk approver**, clearly marked as such in code, in every
   ledger decision it writes and in every result and report. The ledger still links every order
   to a risk decision, so Sprint 12 replaces the approver without changing the ledger.
4. Sprints 7–10 keep their content and follow once real data exists or the owner reorders again;
   nothing in them is dropped.

## Consequences

- The Sprint 11 invariants that matter for the event tier hold from the start: fills at the next
  quote on the correct side after latency, never at mid or the signal bar's close; decisions
  taken while the market is closed place no order (ADR 0032); the entry blackout around the
  rollover is the 16:45–18:15 New York window of C-3 (ADR 0026), not the plan's older 16:55–18:05
  default.
- Some Sprint 12 tasks depend on tasks of the postponed sprints, so Sprint 12's scope under this
  order is an **open question for the owner's Sprint 11 review**:
  - SIGNAL-003 (regime filters) and ROB-006 (pre-registered slicing) depend on REG-007
    (Sprint 8);
  - ROB-004 (Monte Carlo with risk rules) depends on ROB-003 (Sprint 9);
  - ROB-005 (noise injection) depends on ROB-001 (Sprint 9);
  - ROB-008 (robustness report and score) depends on ROB-001 … ROB-007 (mostly Sprint 9).
  RISK-001 … RISK-006, SIGNAL-001, SIGNAL-002, SIGNAL-004 and SIGNAL-005 have their dependencies
  met once Sprint 11 is done (RISK-002 uses the interim sigma-hat of ADR 0044).
- `docs/STATUS.md` records the order and the open question.
