# ADR 0066 — Sprint 7, part 2: the feature library (FEAT-001 … FEAT-006, FEAT-008)

- **Status:** accepted (build-only; readings flagged for the owner's review: C-33)
- **Date:** 2026-10-05
- **Decided by:** Claude, within the plan (Phase 8, Sprint 7) and the owner's instruction for
  this session: FEAT-001 (spec, registry, the leakage harness auto-applied to every registered
  feature so an unchecked feature fails CI), FEAT-002 … FEAT-006 and FEAT-008; no global
  scalers; RSI, MACD and ATR checked against hand computations; no feature selection or importance
  on any data; synthetic data only
- **Tasks:** FEAT-001, FEAT-002, FEAT-003, FEAT-004, FEAT-005, FEAT-006, FEAT-008

Synthetic data only. No feature set has been materialized on real data, nothing was selected,
ranked or tuned on data, and nothing here is evidence. FEAT-007 (gated on DATA-011), FEAT-009
(external series) and FEAT-010 (diagnostics, Sprint 8) are not part of this session.

## FEAT-001 — the framework

1. **A feature** (`xq.features.base`) is a registered, versioned, pure function
   `compute(bars, params, context) -> DataFrame` of the complete bars of one timeframe: one row
   per bar indexed by the bar's `available_at`, using that bar, earlier bars and the calendar
   only. `register_feature(name, version, family, params, lookback, warmup)` validates the name
   (lower snake case, never `tgt_`/`fwd_`), the code version (≥ 1) and the family (`price`,
   `momentum`, `volatility`, `structure`, `time`) and returns an immutable `Feature`;
   `lookback` and `warmup` are functions of the parameters. Parameter models are frozen pydantic
   models without unknown keys and without research defaults: **every parameter value lives in
   `config/features.yaml`**.
2. **A `FeatureSpec`** is one configured use: the plan's
   `FeatureSpec(name, version, family, timeframe, params, lookback, warmup, inputs)` plus its
   output column (default `<name>_<parameter values>`; a multi-column feature adds `_<sub>`).
3. **The registry** (`xq.features.registry.FEATURES`) is a static `MappingProxyType` built from
   the family modules' tuples (no module-level mutable state; a duplicate name is refused).
4. **Feature sets** are named and versioned in `config/features.yaml` (`FeatureSetConfig`:
   `description`, `base_columns`, `features`). `compute_feature_set(specs, inputs, context)`
   computes a set from the dataset builder's input frames; `resolve_feature_set(cfg, ref)` gives
   the builder a `FeatureSetDef` for a configured set as for the built-in `base.v1` (the vault
   evaluation computes features the same way).
   - With `base_columns: true` a set also carries `base.v1`'s columns (decision bar, context bars,
     calendar), so the board's signal bars (`ctx_<tf>_*`) stay available.
   - **Identity.** A configured set's definition, the framework's code version and every used
     feature's code version are hashed into the dataset's config digest and its code versions
     (`features:<set>`, `feature:<name>`), and **locked** in the new `feature_sets` table
     (migration 0017, as `target_sets`) on first use: a changed definition under the same version
     is refused (`FeatureSetChangedError`).
5. **The leakage harness, auto-applied** (`tests/leakage/test_all_features.py`): every spec of
   every configured feature set is computed alone through `compute_feature_set` (the builder's
   own path, joins included) on eight synthetic weeks of 15m bars with 1h, 4h and 1d context
   bars, must produce finite values, and must pass truncation invariance, future perturbation and
   the availability audit. `unchecked_features(cfg)` lists registered features that no
   configured set uses, and the suite fails on any: **a feature cannot be registered without
   being checked.** A planted leak (the next bar's close) is caught on the base timeframe and as
   a multi-timeframe feature, and an unused registered feature is reported.
6. **Normalization** (the plan's rule): no global scalers. Features are scaled by trailing
   statistics only (`bar_sigma`, the timeframe's EWMA volatility of one-bar log returns known at
   the bar; `trailing_zscore`), or at model time by a `TrainingFoldScaler` fitted on one training
   fold's rows and applied to the others (tested: test rows never move the scale).
