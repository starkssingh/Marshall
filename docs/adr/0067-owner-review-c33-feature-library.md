# ADR 0067 — Owner review of Sprint 7 part 2's readings (C-33)

- **Status:** accepted
- **Date:** 2026-10-05
- **Decided by:** project owner, reviewing the readings of ADR 0066 (C-33); (3), (4) and the
  dataset spec implemented by Claude. Amends ADR 0066
- **Tasks:** FEAT-001, FEAT-002, FEAT-006, FEAT-007, DS-005

Synthetic data only. No feature set has been materialized on real data.

## (1), (2) and (5) — approved

1. `core.v1`'s conventional parameter values stand as fixed before any result.
2. Sigma units come from each timeframe's own trailing EWMA volatility (`bar_sigma`); VOL-006's
   per-fold sigma-hat stays a model-time quantity.
5. Calendar-based features (sessions, events, prior session, trading-day weekday) use
   `config/sessions.yaml`. **If the DQ-008 review changes the calendar, every feature set whose
   features read it gets a new version** (`core.v2`, ...); `core.v1` is never recomputed on a
   changed calendar (the dataset's config digest already covers the calendar, so its id would
   change too).

## (3) No filling; the dataset's feature warm-up reads pre-start bars

- **Nothing is filled.** A feature is missing until its warm-up is met.
- **The warm-up is automatic.** For each input timeframe a configured feature set reads (the base
  bars and each context timeframe), the warm-up is the longest `max(lookback, warmup)` among its
  specs on that timeframe (`xq.features.registry.warmup_bars`). For `core.v1`: 231 base 15m bars
  (EMA 100's 99 % weight), 77 1h bars, 91 4h bars and 77 daily bars (Wilder's RSI 14 and ATR 14).
- **Same rules as C-29** (ADR 0064): the builder reads those bars from before the dataset's
  start — the same source, bar build and price basis, complete bars only, without the days the
  spec excludes, never from the vault (the catalog enforces it) — and every trading day they touch
  goes through the dataset's quality gate (a FAIL or unvalidated day refuses the build, as for any
  bars). It reads the last N usable bars available by the first decision
  (`start + base bar + publication latency`), from the open trading day the count needs plus
  `WARMUP_MARGIN_DAYS` (5, shared with the board's rule warm-ups), and keeps no more.
- **Missing bars are an error**: fewer than N usable bars stop the build with a
  `FeatureWarmupError` naming the timeframe and the counts.
- The spec's own `warmup` (10 days for ds_base) still applies (sigma-hat for the targets, the
  context bars of `base.v1`); the builder reads from the earlier of the two. `base.v1` reads
  nothing more.
- `FRAMEWORK_VERSION` goes from 1 to 2, so every dataset id of a configured feature set changes;
  `base.v1` datasets keep theirs.
- **Known truth** (`tests/integration/datasets/test_feature_set_dataset.py`, seventeen synthetic
  weeks with Labor Day excluded): the warm-up per timeframe equals the longest lookback of its
  specs; **`core.v1` has no missing value in any of its columns from the dataset's first trading
  day**; a dataset starting in August (18 daily bars of history for a 77-bar warm-up) is refused;
  without the Labor Day exclusion the quality gate refuses the build, because that day lies inside
  the daily warm-up.

## (4) The VWAP distance is gated out of model inputs until FEAT-007

- A registered feature may declare an **admission gate** (`GATES` in `xq.features.base`):
  `session_vwap` waits for `tick_volume` — FEAT-007's admission of tick-count weights (cross-feed
  stability, DATA-011).
- A feature set lists gated features under `gated:` (`FeatureSetConfig.gated`); a gated feature
  under `features:`, or an ungated one under `gated:`, is a configuration error. Gated features
  are computed, stored, warmed up and run through the leakage harness like the others.
- **Model inputs** come only through `xq.features.registry.model_inputs(features, definition,
  requested)`: by default every column of the set's ungated features (never base or provenance
  columns); a request naming a gated feature's column raises `GatedFeatureError`, naming the gate
  and FEAT-007. No gate is admitted today; FEAT-007 records an admission when it is built.
  Columns are attributed to the spec with the longest matching output name, so the one-hot
  `session_*` columns and `session_vwap_96` are told apart.
- `core.v1` changes in place by this decision (the VWAP instance moves to `gated:`), before any
  build on real data; its definition hash changes with it.
- **Known truth** (`tests/unit/features/test_gates.py`, the dataset test): the VWAP distance is
  `core.v1`'s only gated feature; requesting it as a model input is refused, the default inputs
  exclude it, and the built dataset still stores it with no missing value.

## Dataset specs

- `ds_base` stays on `base.v1` (the baseline board, H-0001).
- `experiments/configs/ds_core.yaml` is `ds_base` with `feature_set: core.v1` and its own name: the
  same source, window (2015-01-01 to `vault.start`), bars, warm-up and target set. **It is a spec
  only: it is not built until the DQ-008 review of the real data.** Its daily warm-up (77 bars)
  comes from the 2014 download.
- **Known truth** (`tests/integration/datasets/test_builder.py`): `ds_core` equals `ds_base` in
  every field but its name and feature set, and every timeframe `core.v1` reads is one of its
  context timeframes.

## Consequences

- Every dataset of a configured feature set warms up on pre-start bars and has a new id
  (`FRAMEWORK_VERSION` 2); `base.v1` datasets are unchanged.
- The 2014 download must cover `core.v1`'s 77-bar daily warm-up as well as the board's rules
  (C-29), and pass `xq validate`.
- Models read features only through `model_inputs`; ML-002's pipeline does.
