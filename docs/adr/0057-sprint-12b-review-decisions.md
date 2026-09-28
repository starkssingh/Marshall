# ADR 0057 — Sprint 12 B review decisions (C-25)

- **Status:** accepted
- **Date:** 2026-09-28
- **Decided by:** project owner, at the Sprint 12 B review (C-25); amends ADR 0023 (trial
  clustering), ADR 0054, ADR 0055 and ADR 0056
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

## 2. Trial clustering uses the absolute correlation

**Decision.** Trials are clustered on the absolute correlation of their daily returns
(|ρ| ≥ 0.7 on average, at least 60 common trading days), so mirror-image rules form one cluster.
A family holding a rule and its mirror has the same effective N and the same Sharpe variance as
the rule alone.

**Why.** A rule and its mirror (the same signal traded the other way) are one choice: which side
to take. Counted as two independent trials they doubled N, and their Sharpe ratios `s` and about
`-s` inflated the variance of the trial Sharpe ratios that sets the deflated Sharpe ratio's
benchmark. The simulated genuine family with its mirrors added had a DSR of 0.007–0.31 over
seeds 0–5, against 0.95–1.00 for the rules alone (ADR 0056 found the same with a hand-built
mirror family).

**Implementation** (`xq.tracking.trials`).

- `cluster_trials` clusters by average linkage on `1 - |ρ|`, cut at `1 - 0.7`, with the frozen
  parameters of `experiments.trial_clustering` (unchanged). Pairs with fewer than 60 common days
  and trials without returns still count as independent.
- **The Sharpe variance follows the clusters.** Equal N is not enough: the variance over raw
  trials still counts the mirror's `-s`. The variance is now taken across clusters, one value per
  cluster, so it matches the count it is used with (the DSR's benchmark is the expected maximum
  of N independent trials with variance V):
  - each cluster's value is the mean recorded Sharpe ratio of its members that trade in the same
    direction as its anchor, the first of its trials in recording order with a Sharpe ratio;
  - a member trades in the anchor's direction when its returns correlate positively with it;
  - a mirror's Sharpe ratio is left out, not negated. A negated net return would count its costs
    as income. A first version negated it, and the variance of the genuine family with its
    realistic mirrors fell to half that of the rules alone (0.112 against 0.219 on seed 0), the
    lenient direction;
  - a trial without returns is its own cluster with its own Sharpe ratio.
- The registry's `trial_count` and the subject's `family_trials` share this code, so a recorded
  run and a simulated subject are counted alike.

**What changed for existing families.** Families without mirrors keep their effective N. Their
Sharpe variance moves slightly, because near-duplicates now contribute their mean once instead of
each contributing its own value:

| Simulated family | Effective N | Sharpe variance before → after | DSR of the candidate before → after |
| --- | --- | --- | --- |
| genuine, seeds 0–5 | 6–7 (unchanged) | 0.116–0.408 → 0.118–0.353 | 0.919–1.000 → 0.953–1.000 |
| overfit, seeds 0–5 | 50 (unchanged) | unchanged (every trial its own cluster) | unchanged, 0.148–0.336 |
| genuine with its mirrors | 12–14 → 6–7 | 0.550–1.412 → equal to the rules alone | 0.007–0.310 → equal to the rules alone |

The fixture expectation in `test_trials.py` changed with the definition: its three
near-duplicates now contribute their mean Sharpe ratio once (variance of 0.1 and 0.5, where it was
the variance of 0.0, 0.1, 0.2 and 0.5).

**Known truth** (`tests/integration/tracking/test_trials.py`). Five rules net of costs, two of
them near-duplicates, and their mirrors, which pay the same costs:

- the rules alone and the rules with their mirrors both have 4 clusters, each mirror in its
  rule's cluster with the opposite sign;
- the Sharpe variance with the mirrors equals that of the rules alone, in the clustering and
  through the registry's `trial_count` of two recorded families.
