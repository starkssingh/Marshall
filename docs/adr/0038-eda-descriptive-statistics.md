# ADR 0038 — EDA descriptive statistics

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** EDA-002, EDA-003, EDA-004, EDA-005 (on EDA-001)

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

## Decision — EDA-004 seasonality

- Families: New York hour of the week (1h bars), day of week and month (daily bars), sessions and
  overlaps, and event windows (the dataset's US-release and rollover windows plus the LBMA AM and
  PM auctions, −5/+30 minutes, on 5m bars). Variables: return, absolute return, tick count and
  spread.
- Effect = bucket mean − overall mean, also in overall standard deviations.
- Standard errors are cluster-robust by trading week (by calendar month for `month`). Intervals
  use the Student-t quantile with G − 1 degrees of freedom (G clusters in the bucket) at
  1 − alpha / (2m), Bonferroni over the m buckets of a family (family-wise alpha 0.05). The t
  quantile keeps buckets with few clusters from looking precise; a zero standard error is never
  significant.
- Split-half stability: the trading days are split at the median and the effect re-estimated in
  each half; `stable` needs the same sign and no significant difference between the halves.
  `unstable` and `insufficient data` are labelled; the report lists the significant **and** stable
  effects separately. Nothing here is validated out of sample.
- Tests: injected hour-of-week mean and volatility effects are significant and stable; noise stays
  within the family-wise rate; an effect present only in the first half is unstable.

## Decision — EDA-005 trend and reversion

- Variance ratios VR(q) of overlapping q-bar sums (Lo–MacKinlay) with the
  heteroskedasticity-robust z*, on 15m returns for q = 2, 4, 16, 92 (30 minutes to one trading
  day). Descriptive only: no verdict is drawn.
- Sign runs of 1h returns against independent signs (Wald–Wolfowitz expectation and z), with run
  length counts against geometric run lengths.
- Buy-and-hold drawdowns of the daily mid close (no costs): every episode with peak, trough,
  recovery, depth and durations; the maximum drawdown, the share of days under water and the
  longest underwater spell.
- Tests: VR matches the estimator written out and the AR(1) theory `1 + 2 sum (1 - k/q) phi^k`;
  GARCH noise has VR near 1 with |z*| < 3; runs detect persistence and reversal; the drawdown
  episodes of a known path are exact.

## Consequences

- Every EDA number has a documented estimator and a simulation test, before it is ever computed on
  gold.
- The hypotheses backlog (`docs/research/hypotheses-backlog.md`) is written only after the first
  real-data EDA; stable, significant effects are candidates, never conclusions.
