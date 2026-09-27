# ADR 0046 — Owner decisions at the Sprint 6 review

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, reviewing PR #11 (Sprint 6, build-only)
- **Tasks:** VOL-006, STAT-006, VOL-005, EXP-004 (trial counting), BASE-003

## Decisions

1. **VOL-006: Holm across challengers.** The one-sided Diebold-Mariano p-values of all
   challengers against the default are Holm-adjusted as one family before `dm_alpha` is applied
   (ADR 0044, amendment). With twelve challengers equal to the default in truth, promotion must
   happen in about 5 % of samples or fewer, not about 40 %; a simulation test pins it.
2. **Separate trial families for forecasting models.** Trials of linear forecasting models
   (STAT-006, the ARMA study) and of volatility models (VOL-005, the volatility board) are
   recorded in their own families, `linear_forecasts` and `volatility_models`
   (`xq.tracking.trials`), fixed by the code whatever the run's hypothesis, and never in a
   trading-strategy family. A strategy's deflated Sharpe ratio therefore counts the strategies of
   its family (e.g. `baselines`), not the forecasting models studied beside it; a test shows the
   `baselines` family's trial count and effective N unchanged when both studies record trials.
3. **The trial rules are approved with separate families.** STAT-001, STAT-002 and STAT-003
   record no trials (descriptive, under H-0000); STAT-006 records one trial per (model, horizon)
   and the volatility board one per model, in the families above (ADR 0043, ADR 0044).
4. **`ar1` stays off H-0001.** H-0001's board keeps its trial budget of 36 (ADR 0035, ADR 0041);
   `ar1` remains available to benchmark boards (ADR 0045). It gets its own pre-registered
   hypothesis, **H-0002 (linear predictability)**, only if STAT-002 or STAT-003 finds dependence
   in returns on real data (the discovery window). H-0002 is not written now.

## Consequences

- `arma_study` and `evaluate_forecasters` no longer take a `family_id`.
- C-19 is closed by decision 4; C-18 keeps the research half of Sprint 6, with the trial rules no
  longer awaiting review, and gains the H-0002 condition.
