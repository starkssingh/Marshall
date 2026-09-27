# ADR 0043 — Statistical time-series methods and recovery before use

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** STAT-001, STAT-002, STAT-003, STAT-006, STAT-008 (Sprint 6, build-only)

## Context

Phase 5 asks for formal tests of stationarity, dependence and linear predictability, each ending
in a written verdict, and requires recovery on simulated processes "before any gold run". The
owner made Sprint 6 build-only (no real data): every method must pass a recovery test on a
simulated process with a known answer before anything else uses it; no reports on real data; no
model promoted.

## Decisions

1. **Libraries.** `arch` (unit-root tests, GARCH family) and `statsmodels` (Zivot-Andrews with its
   break date, ARMA estimation) become runtime dependencies; `statsmodels` was a development
   dependency (the EDA reference in tests). Ljung-Box, the robust portmanteau and ARCH-LM are a
   few lines each and are computed directly (tested against statsmodels), which avoids the
   FutureWarnings of statsmodels' tuple-returning functions under `filterwarnings = error`.
2. **Typed results.** Every test returns a `StatResult` (statistic, p-value, lags, observations,
   null, alternative, assumptions, level, decision, details). Library warnings raised while a
   test runs are recorded in its `notes`, not silenced; the one exception is arch's KPSS notice
   that its default bandwidth is now data-dependent (that default is used on purpose).
3. **Parameters** live in `config/stats.yaml` (`AppConfig.stats`), fixed before any real result;
   changing one afterwards needs an ADR.
4. **STAT-001.** ADF (AIC lags), Phillips-Perron, KPSS (level and trend; the level one enters the
   verdict) and Zivot-Andrews (one level break, 15 % trimming) with the joint verdict table of
   `xq.research.stats.stationarity` (stationary, unit root, conflicting, inconclusive, mixed).
   Non-rejection is never read as proof of a unit root; a Zivot-Andrews rejection is reported as
   "may be stationary around a break at <date>", from one search for one break.
5. **STAT-002.** Ljung-Box on returns, absolute and squared returns; on returns also the
   heteroskedasticity-robust portmanteau Q* (Diebold 1986), since the plain Ljung-Box over-rejects
   under volatility clustering (a test shows it); ARCH-LM on demeaned returns. Each (test, series)
   family carries Holm-adjusted p-values across its lags, and verdicts read those. An ARCH-LM
   rejection is volatility clustering, never return predictability.
6. **STAT-003.** Lo-MacKinlay robust z* at 2 ... 64 bars and the Chow-Denning joint test (a
   horizon counts only if the joint test rejects). By slice (session, volatility regime) q-bar
   windows stay inside contiguous runs of the slice, and slices' joint p-values are
   Holm-adjusted. Volatility-regime labels use the trailing volatility known before each return,
   with cut-offs from reference rows the caller names (training or discovery rows).
7. **STAT-006.** ARMA(p, q) of 1-bar log returns (= ARIMA(p, 1, q) of log price with drift) by
   exact maximum likelihood on each training fold only (returns scaled to unit variance for the
   optimizer); `ar_aic` chooses p by AIC on the training fold. Forecasts of the next h bars' sum
   filter innovations forward from zero with the fitted parameters, so they are causal by
   construction. As a walk-forward estimator (`ArmaForecast`) the model reads the decision bar's
   own return, `log(close / open)`, learns from the training rows' returns (not the targets) and
   starts its filter at each fold's first test row. `arma_study` runs models and benchmarks
   (`zero_return`, BASE-001's `random_walk` where the horizon's bar exists) through the runner on
   identical folds and compares them by Diebold-Mariano on squared errors with h-bar
   autocovariances; one-sided p-values are Holm-adjusted across horizons per (model, benchmark).
   **Useful evidence** is a Holm-adjusted p below `alpha` against every benchmark at some horizon;
   in-sample coefficients (first training fold) are recorded, never promoted. Inside a run each
   (model, horizon) is one trial on test folds; benchmarks are references, not trials. SARIMA is
   not built (EDA-004 has found no stable daily cycle on real data yet).
8. **STAT-008.** A verdict per method and series, with every field of Observed / Evidence /
   Interpretation / Limitations / Action required and the recovery tests of its method cited;
   statuses are `useful evidence` (out of sample, after Holm, STAT-006 only), `evidence`,
   `no evidence` and `inconclusive`. The report is the deterministic `ReportBuilder` output
   (`verdict.md`, a summary table, the raw result tables). It is a framework only in Sprint 6: it
   has run on simulated results, never on real data.
9. **Recovery before use.** `xq.research.recovery.RECOVERY_TESTS` names, per method, the tests
   that recover a known answer on a simulated process (random walk, AR(1), level shift,
   GARCH(1,1), Ornstein-Uhlenbeck); a test checks that each named test exists. A method without
   an entry must not be used by a report, a board or the sigma-hat selection.

## Consequences

- The statistical methods are implemented and tested on simulated processes only; nothing is
  validated on real data. No statistical report exists.
- The plain Ljung-Box on returns is kept for comparability with the literature, but only the
  robust Q* may support a claim of return autocorrelation.
- **Trials (Claude's reading, for the owner's review with C-18).** STAT-001, STAT-002 and STAT-003
  are descriptive tests on the discovery window: like EDA they evaluate no trading configuration
  and record no trials, and would run under the descriptive hypothesis H-0000 (ADR 0041). STAT-006
  and the volatility board (VOL-005) evaluate models on walk-forward test folds, so each (model,
  horizon) is one trial of the run's hypothesis family; their benchmarks are references, not
  trials.
