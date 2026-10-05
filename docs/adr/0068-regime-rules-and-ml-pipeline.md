# ADR 0068 — The last data-independent tasks: REG-001, ML-001, ML-002, ML-003, ML-009

- **Status:** accepted (build-only; readings flagged for the owner's review: C-34)
- **Date:** 2026-10-05
- **Decided by:** Claude, within the plan (Phase 7, Phase 11) and the owner's instruction for this
  session: REG-001 (rule-based volatility, trend and compression regimes, cut-offs from training
  folds only), ML-001 (the Forecaster protocol and wrappers), ML-002 (the in-fold pipeline with
  purged inner CV, embargo, fold-fitted transforms and calibration on validation, including the
  purging demonstration), ML-003 (seeded Optuna HPO, every evaluated configuration a trial),
  ML-009 (persistence and model cards; reload within 1e-9); synthetic data only
- **Tasks:** REG-001, ML-001, ML-002, ML-003, ML-009

Synthetic data only. No regime has been cut and no model trained on real data, and nothing here
is evidence.

## REG-001 — rule regimes (`xq.research.regimes.rules`)

1. **Three rules on `core.v1` columns** (`config/regimes.yaml`):
   - **volatility**: `ewma_sigma_96` below its first training quantile (0.3333) is `low`, at or
     above the second (0.6667) `high`, otherwise `mid`;
   - **trend**: `trend_up` / `trend_down` (by the sign of `ma_slope_t_20`) when
     `efficiency_ratio_20`, `adx_14_adx` and `|ma_slope_t_20|` are all at or above their training
     quantiles (0.6667, 0.6667, 0.5), otherwise `range`;
   - **compression**: `vol_ratio_16_96` and `compression_20_96` (the band-width percentile) both
     at or below their 0.25 training quantiles is `compression`, both at or above their 0.75
     quantiles `expansion`, otherwise `normal`.
2. **Cut-offs from training folds only.** `fit(train)` computes quantiles on a training fold's own
   rows (at least `min_training_rows`, 100, with every input); `filter(data)` labels rows with
   those fixed cut-offs, row by row, so it is causal whenever its inputs are. `fit_per_fold`
   refits on every walk-forward fold's training rows and labels that fold's test rows; a fold with
   too few training rows labels nothing. No cut-off is computed on the full sample or on test rows.
3. **Output** follows REG-007's contract: `state` (code), `label`, a one-hot `p_<state>` per state
   (a rule is certain), `regime_age` (rows since the state changed; a missing input, or a new
   fold, starts a new run). A row with a missing input has no state. `states()` describes them.
4. The rules are not features yet (REG-007 exports them and runs the harness); REG-006 evaluates
   them. Nothing was cut on data.
5. **Known truth** (`tests/unit/research/test_regime_rules.py`): the configured columns are
   `core.v1` columns; terciles and labels by hand; the trend needs every measure and takes the
   slope's sign; compression and expansion need both measures; labels are truncation invariant;
   each fold's cut-offs equal the quantiles of its own training rows, are unchanged when that
   fold's test rows change (later folds, which train on them, do change); too few training rows
   label nothing.
