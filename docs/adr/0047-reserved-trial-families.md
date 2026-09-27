# ADR 0047 — Reserved trial families cannot be hypothesis families

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner (start of Sprint 11), closing the gap left by ADR 0046
- **Tasks:** EXP-002 (hypothesis pre-registration), EXP-004 (trial counting)

## Context

ADR 0046 records the trials of forecasting-model studies in their own families,
`linear_forecasts` (STAT-006) and `volatility_models` (VOL-005), fixed by the code. The studies'
own trials can never land in a trading-strategy family, but nothing stopped a trading-strategy
hypothesis from being *registered* with one of those family ids. Its trials would then be counted
together with model evaluations, and the model evaluations would deflate (or dilute) the
strategy's Sharpe ratio: exactly the mixing ADR 0046 set out to prevent. `docs/STATUS.md` listed
this under known issues.

## Decision

1. The reserved family ids are listed in one place, `xq.tracking.registry.RESERVED_FAMILIES`
   (currently the model families `linear_forecasts` and `volatility_models`). Any future reserved
   id is added there. The family constants move from `xq.tracking.trials` to
   `xq.tracking.registry`, the lowest tracking layer, so that the registry itself can check them.
2. Registration refuses a reserved family twice over: the hypothesis schema (`HypothesisDoc`)
   rejects it when the YAML is loaded (`xq exp register` exits with a configuration error), and
   `registry.add_hypothesis_version` raises `RegistryError` for it, whoever calls it.
3. Only exact ids are reserved; families that merely resemble them (`volatility_model`) are
   ordinary families.

## Consequences

- No registered hypothesis can share a family with forecasting-model trials; `record_trial` still
  accepts the reserved families, since that is how the studies record their own trials.
- Imports of `LINEAR_FORECAST_FAMILY`, `VOLATILITY_MODEL_FAMILY` and `MODEL_FAMILIES` come from
  `xq.tracking.registry`.
