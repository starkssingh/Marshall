# ADR 0057 — Sprint 12 B review decisions (C-25)

- **Status:** accepted
- **Date:** 2026-09-28
- **Decided by:** project owner, at the Sprint 12 B review (C-25); amends ADR 0054, ADR 0055 and
  ADR 0056
- **Tasks:** VAL-002, VAL-003, VAL-004, EXP-004, ROB-001, ROB-008, `xq validate-strategy`

Everything here runs on synthetic data and simulated strategies only. Nothing has run on real
data.

## Context

ADR 0055 and ADR 0056 left eight open points for the owner (C-25). The owner decided four of them
and approved three as they stand. The remaining point, the baseline-board subject adapter for
`xq validate-strategy`, is the next build task (ADR 0058).

**Approved as they stand:**

- a reproduction on the same code with a metric out of tolerance has its own status,
  NOT_REPRODUCED, never counted as reproduced (ADR 0055);
- a validation run records no trials: it selects nothing (ADR 0056);
- the Monte Carlo calls a path ruined at 50 % of the capital (still provisional) and keeps gap
  losses at their observed size (ADR 0056).

The volatility-tercile `no_sigma_hat` bucket (ADR 0056) was not raised and stands as implemented.

The four decisions follow, each with how it is implemented and what it showed. Each is one commit
with its tests.

## 1. PBO does not apply to a family without a meaningful selection

**Decision.** If the family's effective trial count is at most 2, PBO is reported as "not
applicable: no meaningful selection" and the R2 PBO criterion is not applicable. The deflated
Sharpe ratio still applies.

**Why.** PBO judges the choice among configurations, not the edge (ADR 0056). Among
near-identical configurations which one wins in sample is a coin toss even when every one has
the edge, so PBO sits near 0.5 and a genuine edge fails `pbo_max` (0.20). With at most two
effective trials there is no selection for PBO to judge. The selection that does exist is still
charged by the deflated Sharpe ratio, whose trial count and Sharpe variance are unchanged by this
rule.

**Implementation.**

- The count is the family's effective trial count as EXP-004 counts it (`TrialSummary.n_effective`:
  the registry's clustered count for a recorded run), whatever `conventions.trial_count` gates the
  DSR on. The limit, 2, is `pbo.not_applicable_max_effective_trials` in `config/validation.yaml`,
  next to the CSCV block count (16, moved there from a code constant).
- A new category, **not applicable**, sits beside "not evaluated". A criterion is not applicable
  only by a rule the owner decided (this one, and the parameter neighbourhood of decision 4). It is
  listed with its reason in the summary, the Markdown and the JSON (`not_applicable`) and does not
  enter the verdict. A criterion that merely could not be computed stays "not evaluated" and makes
  the verdict `incomplete`, as before (ADR 0056): the new category is never a way around a gate.
- CSCV still runs when it can, and its value is shown "for reference, not judged". The
  `stat_tests` row of PBO records `applicable` and the effective count.

**Known truth** (`tests/unit/validation/test_significance.py`).

- The simulated genuine edge selected from its near-identical configurations only (lookbacks
  40–80 days, 9 configurations) has 1 effective trial on seed 0 and a CSCV PBO of 0.52, which fails
  `pbo_max`. Its PBO is now not applicable, its DSR is still judged, and its significance
  criteria pass. Over seeds 0–7 this family had 1 or 2 effective trials and PBO between 0.19 and
  0.94.
- The single-point optimum on noise (50 independent configurations) keeps PBO applicable and
  still fails it.
