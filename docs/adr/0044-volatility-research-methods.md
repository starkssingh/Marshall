# ADR 0044 — Volatility research methods, evaluation on identical folds and sigma-hat selection

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** VOL-001, VOL-002, VOL-003, VOL-004, VOL-005, VOL-006, BASE-003 (Sprint 6, build-only)

## Context

Phase 6 looks for the estimators and forecasters that best predict realized variance out of
sample, and promotes one as the platform's sigma-hat source (VOL-006), or keeps EWMA/HAR if nothing
beats them. The owner made Sprint 6 build-only: every method passes a recovery test on a simulated
process before anything uses it (ADR 0043's registry), the diurnal factor is fitted on training
folds only, VOL-006 defaults to EWMA when nothing beats it, no report runs on real data and no
model is promoted. Parameters live in `config/volatility.yaml` (`AppConfig.volatility`), fixed
before any real result; changing one afterwards needs an ADR.

## Decisions

1. **VOL-001 range estimators.** Close-to-close, Parkinson, Garman-Klass, Rogers-Satchell and
   Yang-Zhang as trailing per-bar variances over `estimators.window` bars (sigma per bar in the
   table, never annualized), on any price basis (plain or `bid_` / `ask_` / `mid_` columns).
   Yang-Zhang is the one that includes opening jumps (the daily break, weekends). Wilder's ATR is
   reported in price units and relative to the close (`atr_rel`); thresholds must use the
   relative or volatility form, never dollars.
2. **VOL-002 realized measures.** RV, bipower variation (with the n / (n - 1) scaling) and the
   jump component per UTC hour and per trading day from 1m or 5m close-to-close returns **inside
   one trading day**: the return across the daily break and weekends is not intraday and stays
   out of every realized measure (Yang-Zhang carries it). A period's row is indexed by its
   decision time, the later of the period's end and its last bar's availability. Bipower is
   jump-robust only as sampling gets finer; a test pins its finite-sample contamination.
3. **Diurnal factor on training rows only.** Buckets are minutes since the trading day's start
   (17:00 New York, so DST is handled by construction); with `day_standardized` each value is
   divided by its trading day's mean first; buckets with fewer than `min_count` training rows keep
   1; factors are normalized to average 1 over the training rows. `DiurnalFactor.fit` takes a
   `train_end` and reads only rows available by then, so rows after it may be passed and are
   ignored; a test perturbs the test rows and finds the factor unchanged, while a full-sample fit
   moves.
