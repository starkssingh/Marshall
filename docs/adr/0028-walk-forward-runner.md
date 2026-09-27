# ADR 0028 — Walk-forward runner, model interface and fold results

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** WF-002, WF-003 (on WF-001, EXP-003; used by BASE-005)

## Context

WF-002: fit → select → predict per fold; process-parallel folds with deterministic per-fold seeds;
caching keyed by dataset id, model configuration hash and fold id. Acceptance: the same result
serial and parallel. Phase 12 tests: a synthetic AR(1) signal gives the analytically expected hit
rate; overlapping labels with no signal give chance-level results under purging. Registry:
`fold_results(run_id, fold_id, train_start, train_end, test_start, test_end, metrics_json)`.

## Decision

1. **Model interface** (`xq.models.base`). A `ModelSpec` has a name, a `code_version`, a task
   (`regression`, or `classification` meaning "is the target positive") and a factory that builds
   an `Estimator` (`fit(x, y, rng)`, `predict(x)`). A `ModelConfig` names the family and gives its
   fixed parameters, an optional grid and the feature columns it reads. Models forecast; they never
   size positions.
2. **Per fold:** unlabelled rows are dropped from training and validation. Each grid candidate is
   fitted on the training rows and scored on the validation rows (MSE, or log loss for
   classification); the lowest loss wins, the first on ties. The selected candidate, fitted on
   the training rows only, predicts the test rows. It is not refitted on training plus validation
   (WF-004 may revisit this). With one candidate or no validation rows, nothing is selected.
3. **Determinism.** Each fold's seed is `derive_seed(base seed, "fold", fold_id)`, and the base
   seed of a recorded evaluation is derived from the run seed and the evaluation label. Folds
   running in `spawn` worker processes give exactly the serial result; tests assert this.
4. **Caching.** A fold's output is cached under `data/cache/walkforward/` with a key covering the
   model configuration hash (including `code_version`), the candidate list, the fold id and
   boundaries, the fold seed, and a hash of every row the fold reads. So a hit can only return
   what recomputing would. This is stricter than the plan's key (dataset id, model hash, fold id).
   Bumping a model's `code_version` whenever its behaviour changes is mandatory; `use_cache=False`
   always recomputes. The metadata file is written last, marking the entry complete.
5. **Recording** (`run_walk_forward`). It runs inside an experiment run and applies the target
   schema guard to the model's feature matrix. It records one `fold_results` row per fold
   (migration 0007) with the selected parameters and the fold metrics. Because a run can evaluate
   several models and targets, the key is `(run_id, evaluation, fold_id)`, with `evaluation`
   defaulting to `<model>:<target>`. Stitched metrics go to `metrics` as `<evaluation>/<name>`. By
   default the evaluation is also one trial on test folds (the whole selection procedure counts
   once, since candidates are compared on validation rows only). A caller that turns the
   forecasts into a strategy records that strategy as the trial instead.
6. **Prediction store (WF-003).** `run_walk_forward` writes the stitched predictions to
   `data/predictions/<experiment_id>/<run_id>/<evaluation>-<hash>.parquet` through
   `write_predictions`. That refuses the whole frame, writing nothing, if any decision time is not
   strictly after its fold's `train_end + embargo`. The file is a run artifact with its SHA-256.
   `model_version` is `<model>@<code_version>:<config hash>`; `feature_set_version` comes from the
   dataset manifest.
7. **Interim metrics.** Folds report `n`, MSE, MAE, sign hit rate and mean forecast (regression),
   or log loss, Brier score and accuracy (classification), until BASE-006 supplies the full
   forecast evaluation.

## Consequences

- The purging demonstration is a test: a model that memorizes the latest training label has a hit
  rate above 0.8 when `label_end` is misreported as the decision time, and is at chance once
  labels are purged.
- Cache entries are never invalidated automatically except through their key. A model change
  without a `code_version` bump would serve stale folds, and code review must catch it.
