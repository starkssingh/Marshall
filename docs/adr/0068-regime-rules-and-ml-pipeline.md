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

## ML-001 — the Forecaster protocol and wrappers (`xq.models.base`)

1. **`Forecaster`** (a protocol): `name`, `task`, `fit(x, y, sample_weight=, eval_set=)` returning
   itself, `predict` (the forecast, or the class 0/1), `predict_proba` (P(y = 1), classification
   only), `save(path)` and `model_card()`. The walk-forward `Estimator` of WF-002 stays as it is
   for the baselines.
2. **`SklearnForecaster`** wraps one scikit-learn estimator of a registered family
   (`forecaster_spec`, `build_forecaster`): `logistic` (elastic net: `C`, `l1_ratio`, saga) and
   `ridge` (`alpha`) in `xq.models.linear`, `random_forest` (shallow, `min_samples_leaf`,
   `max_samples` reduced for overlapping labels) in `xq.models.trees`. It is seeded; refuses
   missing or infinite inputs (the pipeline drops such rows), a classifier target other than 0/1
   or with one class, and negative weights; remembers its feature columns and refuses another
   order at prediction. `eval_set` is accepted by every forecaster and used only by a family that
   stops early on it (none yet).
3. **Dependencies:** scikit-learn (the estimators, isotonic and Platt calibration in ML-002) and
   joblib (the artifact format of ML-009), both added in this sprint; mypy ignores their missing
   stubs as for scipy.
4. **Layout:** `xq.models.linear` and `xq.models.trees` are the plan's files; the family registry
   is a function in `xq.models.base` (`forecaster_spec`), importing them lazily.
5. **Known truth** (`tests/unit/models/test_forecasters.py`): every family conforms (fit with
   weights and an eval set, predictions as float arrays, probabilities in [0, 1] whose 0.5 cut is
   the class, a card, a save and load that predicts the same), learns a planted linear signal, is
   identical under a fixed seed, and refuses what it cannot use.

## ML-002 — the in-fold pipeline (`xq.models.pipeline`, `xq.models.calibration`)

1. **`train_fold(family, fold, data, cfg, embargo=, seed=, ...)`** trains on one walk-forward
   fold's training window only (`config/ml.yaml`, `pipeline`):
   - usable rows have a known target and finite inputs (nothing filled; a test row with a
     missing input stays unpredicted); model inputs come from `model_inputs` (C-33 (4));
   - the last 20 % of the window's rows validate; fitting rows are purged against the validation
     start minus the embargo;
   - sample weights (`uniqueness`): average uniqueness of the fitting rows' labels among
     themselves, mean 1 (TGT-006);
   - hyperparameters are given, or chosen by a search (ML-003) minimizing `inner_cv_loss`: the
     mean log loss (MSE for regression) over a **purged k-fold with embargo** (5 splits) inside
     the fitting rows, purged by `max(label_end, weight_end)` (C-30 (4)), each inner split with
     its own scaler;
   - the model is refitted on every fitting row after a `TrainingFoldScaler` fitted on them (a
     column constant there is set to 0: it carries no information);
   - **calibration on the validation rows only**: isotonic above 1,000 rows, Platt otherwise
     (the plan); ECE and log loss on validation are recorded raw and calibrated;
   - the test rows get `p_raw`, `p_cal` and `y_pred` in the walk-forward prediction columns, with
     `train_end` the latest purge end of any row used.
2. **`run_pipeline`** runs every fold with a seed derived from the run seed and the fold id,
   skips (and lists) folds with fewer than 200 fitting rows, and stitches the test predictions
   (`stitch_oos`). The embargo is the plan's `max(label horizon, 1 trading day)`
   (`default_embargo`).
3. **Calibration weights (a choice for review, C-34).** Validation labels overlap: 200 hourly
   rows of 48-hour labels hold about four independent outcomes. Platt scaling fitted as if they
   were 200 independent rows extrapolated a spurious validation pattern to the test rows (the
   purging demonstration's out-of-sample log loss reached 1.98 against a climatology of 0.65).
   Calibration is therefore weighted by the validation labels' **raw** average uniqueness, and
   Platt's slope carries a unit L2 penalty (the intercept is not penalized), which shrinks the
   map towards the validation base rate when the independent evidence is thin.
4. **The purging demonstration** (`tests/unit/models/test_pipeline.py`; the plan's test): hourly
   samples, two slowly drifting inputs unrelated to the returns, the sign of the next 48 hourly
   returns as the label (overlapping, no signal), a deep random forest. Unpurged **shuffled**
   5-fold CV shows spurious skill (AUC above 0.7, log loss more than 0.1 below climatology; it
   gave 0.76–0.82 and 0.52–0.54 against 0.66–0.68 on three seeds); **purged** 5-fold CV with
   embargo and the walk-forward pipeline are at chance (AUC within 0.1 of 0.5) and the pipeline
   has no out-of-sample skill (log loss not below the out-of-sample climatology). Shuffled splits
   exist only in that test (scikit-learn's `KFold`); the library has none.
5. **Known truth** (the same files and `tests/unit/models/test_calibration.py`): the validation
   split's purge by hand; the embargo default; the scaler, the validation metrics and the
   calibrator unchanged when every test row's inputs and labels change; fitting rows before the
   validation rows and purged against them; determinism under a fixed seed; a missing test input
   unpredicted; inner CV splits purged and embargoed (no training label within a test group's
   span plus the embargo) and at chance on no signal; calibration on validation rows cuts ECE on
   three-times-overconfident scores by more than two thirds (Platt below 1,000 rows, isotonic
   above); weights shrink Platt's map.

## ML-003 — seeded Optuna search, every configuration a trial (`xq.models.hpo`)

1. `optuna_search(family, cfg, seed=, record=)` is a `Search` for the pipeline: Optuna's TPE
   sampler seeded by `derive_seed(seed, "hpo", family, fold_id)`, exactly `n_trials`
   configurations per family and fold (50 by default, the plan's fixed budget), drawn from the
   family's space in `config/ml.yaml` (intervals, log-scaled or integer, or lists of choices) on
   top of its fixed parameters; the objective is the inner purged CV loss on the fitting rows
   (ML-002), so no configuration is scored on validation or test rows. The best evaluated
   configuration is returned; an unusable one (no usable inner split) is reported to the sampler
   as a very large loss and does not stop the search.
2. **Every evaluated configuration is a trial.** `trial_recorder(run, family_id, context)`
   records each as a trial of the hypothesis family with `evaluated_on_test=False` and its inner
   loss, so the trial counter, the effective trial count and the deflated Sharpe ratio see the
   whole search; the chosen model's out-of-sample evaluation is recorded by its caller, on test.
3. **Dependency:** Optuna (the TPE sampler), added in this sprint.
4. **Known truth** (`tests/unit/models/test_hpo.py`, `tests/integration/models/test_ml_trials.py`):
   a seed reproduces every configuration in order and the choice, another seed does not; the
   budget is spent exactly, inside the space (integer, log and choice dimensions honoured, fixed
   parameters carried); the best evaluated configuration is returned; inside a run with three
   walk-forward folds and a budget of 6, the trial counter holds 6 × 3 search trials plus the one
   out-of-sample trial, of which one is on test, and the planted signal is found out of sample.

## ML-009 — persistence and model cards (`xq.models.persistence`)

1. `save_trained_fold(directory, trained, data, dataset_id=, feature_set=)` writes
   `model.joblib` (the forecaster with its training-fold scaler and calibrator) and `card.json`,
   the **model card**: family, code version, task, hyperparameters, seed, input columns,
   feature-set version, dataset id, fold id, training cutoff, fitting and validation row counts,
   validation metrics, calibration method, the SHA-256 of the training data (the fitting rows'
   inputs, targets and decision times), the library versions (Python, NumPy, pandas,
   scikit-learn, joblib, Optuna) and the artifact's SHA-256.
2. `load_model(directory)` refuses an artifact whose bytes no longer match the card, and one
   written with other library versions unless `allow_version_drift=True`; the loaded model
   predicts as the pipeline did (scaler, forecaster, calibrator).
3. joblib unpickles: only artifacts this project wrote are loaded; the hash guards their
   integrity, not their origin. Registering model versions in the registry (MREG-001's
   `model_versions`) from these cards waits for the first real candidate.
4. **Known truth** (`tests/unit/models/test_persistence.py`): for a logistic and a random-forest
   fold, a reload reproduces the pipeline's test `p_raw` and `p_cal` within 1e-9; the card carries
   every field above; the training-data hash changes with a fitting row; a tampered artifact and a
   library-version drift are refused.

## Readings for the owner's review (C-34)

1. **Calibration weights** (ML-002, item 3): validation rows are weighted by their labels' raw
   average uniqueness and Platt's slope carries a unit L2 penalty, so a calibration map fitted on
   a few independent outcomes shrinks towards the base rate. The plan names isotonic and Platt
   without weights.
2. **No filling at model time**: a training row with a missing input is dropped, a test row with
   one is not predicted, and a column constant in the fitting rows is set to 0 after scaling.
3. **Regime cut-off quantiles** (REG-001): terciles for volatility, the upper third for the
   efficiency ratio and ADX, the median of the absolute slope t-statistic, quartiles for
   compression; fixed before any result, as `config/regimes.yaml`.
4. **The HPO budget** counts per fold: a 50-trial search on 40 monthly folds is 2,000 trials in
   the family, which the deflated Sharpe ratio then charges for. The plan's "fixed budget per
   family" could also be read as per family overall.
5. **Version drift refuses a load** unless explicitly allowed (ML-009).

## Consequences

- The data-independent tasks of Phases 7 and 11 that the owner ordered are built; ML-004 onwards
  (Stage A on `ds_core`), REG-006 and REG-007 need real data or the board.
- New dependencies: scikit-learn, joblib, Optuna.
- Speed on real data is unmeasured: the inner CV fits `inner_splits` models per configuration,
  so a 50-trial search per fold fits 250 models per fold before the refit.
