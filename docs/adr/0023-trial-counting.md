# ADR 0023 — Trial counting and effective trials

- **Status:** accepted; the clustering threshold is proposed and fixed before any research result
- **Date:** 2026-09-26
- **Tasks:** EXP-004 (consumed by VAL-002 DSR and VAL-004 SPA)

## Context

EXP-004: a trial counter per family and globally; the effective number of independent trials is
estimated by clustering trial return correlations; the counts feed the deflated Sharpe ratio and
SPA. Acceptance: every test-fold evaluation is counted.

## Decision

1. **Recording.** `record_trial` (also `RunContext.record_trial`) stores one row per evaluated
   configuration of a *running* run: family, configuration hash, whether it was evaluated on test
   folds, its Sharpe ratio if computed, and its return series (Parquet under
   `data/artifacts/<experiment>/<run>/trials/`). Trials cannot be recorded outside a live run and
   are never deleted (ADR 0020).
2. **Counts.** `trial_count(cfg, engine, family)` returns the number of trials, the number
   evaluated on test folds, the effective number of independent trials and the variance of trial
   Sharpe ratios; `family=None` counts across all families. `xq exp trials [--family]` prints
   them.
3. **Effective trials by clustering.** Return series are correlated pairwise (at least
   `min_overlap` = 20 common observations; since ADR 0026, returns are first summed per trading
   day and a pair needs `min_common_days` = 60 common trading days), clustered by average linkage
   on `1 - rho`, and cut at
   `1 - correlation_threshold` with `correlation_threshold` = 0.7: trials whose returns correlate
   above 0.7 on average count as one. Parameters live in `config/base.yaml`
   (`experiments.trial_clustering`); they are proposed values and, like gate thresholds, may not be
   changed once results exist without an ADR.
4. **Conservative defaults.** A trial without returns, or a pair with too little overlap, counts
   as independent. Over-counting raises the bar a candidate must clear; under-counting would
   flatter it.
5. **Dependency.** `scipy` is added in this task for hierarchical clustering; it is also the
   statistics library the plan uses from Sprint 4.

## Consequences

- DSR and SPA in Sprint 4 read `n_trials`, `effective_n` and `sharpe_variance` from the registry
  instead of from the researcher.
- The threshold is a judgement call; the report of any candidate should show both the raw and the
  effective count.
