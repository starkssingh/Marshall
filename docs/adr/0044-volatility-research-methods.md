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
4. **The `VolForecaster` interface** (`xq.models.volatility`, the VOL-006 file) works on a
   periods frame (hours or trading days from VOL-002) indexed by decision time:
   `fit(periods_train)`, `predict_variance(periods_upto_t, h)` — the variance of the next h
   periods' summed return, i.e. of `rv_{t+1} + ... + rv_{t+h}` — and `predict` (sigma-hat, its
   square root). Every forecaster is causal: a test perturbs later rows.
5. **VOL-003 benchmarks**, never tuned: `rolling_22` (mean RV), `ewma_0.94` and `ewma_0.97`
   (RiskMetrics on squared period returns, started at the training mean) and `har` (one OLS per
   horizon on training rows whose h future periods are also training rows; components 1, 5, 22
   trading days, or 1, 23, 115 hours on hourly periods; floored at 1 % of the mean training RV
   because a linear HAR can go negative). On hourly periods every benchmark is wrapped in
   `Deseasonalized`: the diurnal factor fitted on the training periods' RV, the inner model fitted
   on adjusted data, and its forecast turned back into raw variance with the factors of the next h
   buckets of the trading day's cycle (known from the calendar, never read from future rows).
6. **VOL-004 GARCH family** with `arch`: GARCH(1,1), GJR-GARCH(1,1) and EGARCH(1,1) with normal,
   Student-t and skewed-t errors (nine models, `<process>_<distribution>`), refitted per fold on
   the training periods' returns divided by their training standard deviation, with a zero mean
   (configurable). Convergence, persistence and warnings are kept as diagnostics, never hidden.
   Multi-step forecasts are analytic for GARCH and GJR; EGARCH beyond one step is simulated from a
   distribution seeded through `xq.core.seeds` (reproducible). The variance recursion starts from
   arch's backcast of the first 75 periods passed; in walk-forward the frame starts with the
   training data, so every test forecast is causal (a test perturbs later rows). On hourly periods
   the models run on deseasonalized returns through `Deseasonalized`. FIGARCH is not built
   (STAT-004, Sprint 8, has not found long memory).
