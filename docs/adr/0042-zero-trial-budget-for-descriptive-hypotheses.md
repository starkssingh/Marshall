# ADR 0042 — A zero trial budget only for descriptive hypotheses

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner (the H-0000 prerequisite, start of Sprint 6), following ADR 0041
- **Tasks:** EXP-002 (hypothesis pre-registration)

## Context

ADR 0041 decided that EDA runs belong to a standing descriptive hypothesis H-0000 with a zero trial
budget, and that the EXP-002 schema must first accept a zero budget (it required at least one).
A zero budget must not become a way for any other hypothesis to skip the trial count: every
configuration evaluated on test folds is a trial (EXP-004), and the deflated Sharpe ratio is only
as honest as that count.

## Decision

1. `HypothesisDoc.trial_budget` accepts 0 **only** when `family` is exactly `descriptive`
   (`xq.tracking.hypotheses.DESCRIPTIVE_FAMILY`). Every other family still needs a budget of at
   least one; a negative budget is refused for every family. Tests pin both directions, including
   families whose names merely resemble `descriptive`.
2. H-0000 is **not** written or registered now. It is pre-registered alongside H-0001 once real
   data fixes the windows (C-16).

## Consequences

- A descriptive hypothesis with a zero budget can be registered, so `xq research eda` can later
  run under H-0000.
- The trial counter does not compare recorded trials with a hypothesis's budget for any family
  (it never has); EDA records no trials by design (ADR 0036, ADR 0041). Enforcing budgets against
  recorded trials remains open and is not part of this change.
