# ADR 0024 — Target framework

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** TGT-001 (TGT-002 adds forward returns)

## Context

TGT-001: `TargetSpec(name, horizon, price_ref, params)`; outputs `value`, `label_start` (execution
time) and `label_end` (used for purging), stored in `targets.parquet`; a schema guard rejects any
target column in a feature matrix; `target_sets(name, version, spec_json, hash, created_at)`.

## Decision

1. **Definitions are configuration.** Target sets live in `config/targets.yaml` as
   `<name>: <version>: {kind, horizons, price_refs, params}`. A set expands into one `TargetSpec`
   per horizon and price reference (`long`, `short`, `mid`); `params` are validated by the kind.
2. **Kinds are code.** A `TargetKind` provides `expand`, a causal `sigma` (the volatility scale
   per decision time), `compute(spec, quotes, sigma)` returning `value`, `label_start`,
   `label_end` and `scale` per decision time, `lookahead` (how far after t it may read quotes),
   and a `code_version`. Kinds are listed explicitly in `xq.targets.kinds` (no import-time
   registration, so no module-level mutable state).
3. **Locking.** The first build using a target set version records its definition hash in
   `target_sets` (migration 0006); a different definition under the same version is refused. The
   definition hash is also part of the dataset config digest and the kind's code version part of
   the dataset id, so the id pins what the targets mean.
4. **Storage.** `targets.parquet` holds one row per decision time and target (`target`, `value`,
   `label_start`, `label_end`, `scale`), covering every feature row (values missing where a target
   cannot be computed). `target_values(targets, name)` gives one target aligned with the
   features.
5. **Inputs.** Targets are computed month by month from clean ticks read through the catalog,
   from the decision time to `lookahead` later, never past `vault.start`; ticks the bars exclude
   (`bars.exclude_flags`) and ticks of excluded trading days are removed first. The trading days
   those quotes may reach are gated by DQ-007 like every other input. Sigma-hat is computed on the
   gated base bars (warm-up included) and taken at each decision time.
6. **Schema guard.** Before features are written, `check_feature_matrix` refuses any feature
   column that is a target name, a target-frame column (`target`, `value`, `label_start`,
   `label_end`, `scale`) or starts with a reserved prefix (`tgt_`, `fwd_`). Modelling code
   (Sprint 4) calls the same guard when it assembles matrices.

## Consequences

- Purging in walk-forward splits (WF-001) can rely on `label_end`, which the leakage harness
  checks for every target kind (ADR 0016).
- Feature names can never collide with target names; the reserved prefixes are part of the
  naming contract for FEAT-001.
