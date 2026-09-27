# ADR 0038 — EDA descriptive statistics

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** EDA-002, EDA-003 (on EDA-001)

## Context

Phase 4 describes XAUUSD returns without searching for strategies: distributions (EDA-002),
dependence (EDA-003), seasonality (EDA-004) and trend and reversion (EDA-005). Research
validation: statistics match scipy/statsmodels on known inputs; every seasonal effect has an
effect size, a multiple-comparison-corrected interval and split-half consistency, and unstable
effects are labelled. Sprint 5 is build-only (ADR 0035): each method is tested on simulated
processes with known properties. Exact formulas are in the module docstrings.

## Decision — EDA-002 distributions

- Per timeframe (1m to 1d), log returns in bps: mean, standard deviation, skewness and excess
  kurtosis (moment estimators, equal to scipy's `bias=True`), Jarque–Bera (scipy), a maximum
  likelihood Student-t fit for the QQ plots against the normal and the t, the Hill tail index of
  each tail from its largest 5 %, and the moments per calendar year.
- Intervals: stationary bootstrap (1,000 resamples, 95 %), one resample at a time so minute data
  fits in memory. The mean block length is the Politis–White length of the **squared** returns —
  the dependence that matters for moments is volatility clustering — at least five trading days of
  bars, and at most a tenth of the series so every resample holds about ten blocks.
- Tests: moments and Jarque–Bera equal scipy; Hill recovers a Pareto index; the t fit recovers its
  degrees of freedom; the block bootstrap widens the interval of an AR(1) mean by about
  sqrt((1 + phi) / (1 - phi)).

## Decision — EDA-003 dependence

- ACF (biased estimator, by FFT) and PACF (Durbin–Levinson) of returns, |returns| and squared
  returns, up to one trading day of lags (at least 20).
- Bands: the i.i.d. band z/sqrt(n) and the heteroskedasticity-robust band from
  `se_k = sqrt(sum e_t^2 e_{t-k}^2) / sum e_t^2` (Taylor 1984; Lo–MacKinlay's delta_k); lags are
  flagged only outside the robust band. The PACF uses the same band. Formal tests (Ljung–Box,
  ARCH-LM) stay in STAT-002.
- `statsmodels` becomes a **development** dependency, used only as the reference in tests (ACF
  and PACF match it to 1e-10); the library does not import it.
- Tests: an AR(1) is recovered; the robust band equals the i.i.d. band for i.i.d. data and is
  about 1.5 times wider at lag 1 for GARCH(1,1) (alpha 0.15, beta 0.8), where it flags no more
  return lags than the i.i.d. band while still flagging volatility clustering.

## Consequences

- Every EDA number has a documented estimator and a simulation test, before it is ever computed on
  gold.
- The hypotheses backlog (`docs/research/hypotheses-backlog.md`) is written only after the first
  real-data EDA; stable, significant effects are candidates, never conclusions.
