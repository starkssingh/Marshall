# ADR 0031 — Statistical validation: Sharpe inference, PSR/DSR, forecast comparison

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** VAL-001, VAL-002, VAL-005

## Context

VAL-001: Lo (2002) standard errors with autocorrelation adjustment, non-normal (Mertens)
standard errors, bootstrap confidence intervals, minimum track record length. Acceptance: matches
published examples.

## Decision

1. **Units.** Inference is done on per-period (daily) Sharpe ratios and period counts. Annualized
   figures are derived at the end, with `sqrt(P)` or with Lo's eta(q) when returns are serially
   correlated.
2. **Three standard errors** (`xq.validation.sharpe`):
   - i.i.d. normal `sqrt((1 + SR²/2)/n)`;
   - Mertens non-normal `sqrt((1 + SR²/2 − γ3·SR + (γ4 − 3)/4·SR²)/n)`, with γ4 the non-excess
     kurtosis;
   - Lo's GMM/delta-method error with a Newey–West (Bartlett) long-run covariance of the mean and
     variance moments. The default lag is `floor(4(n/100)^(2/9))`.

   Reports show all three; decisions use the largest.
3. **Bootstrap interval:** the percentile interval of the stationary bootstrap (Politis–Romano).
   Blocks have geometric lengths, the mean block length is set by the caller, and the seed comes
   from the run (`xq.core.seeds`).
4. **Minimum track record length** (Bailey & López de Prado 2012), in periods, against a benchmark
   Sharpe ratio at one-sided level α. It is infinite when the observed ratio does not exceed the
   benchmark.
5. **Verification.** The formulas are checked by hand and by their defining property (at MinTRL
   periods the one-sided z statistic equals z_α). The standard errors are checked by Monte Carlo
   against the sampling distribution they claim to describe: normal, negatively skewed and
   fat-tailed, and AR(1) returns. Lo's eta(q) is checked against its closed form for AR(1)
   autocorrelations. No published numerical table was reproduced for VAL-001. The published
   Deflated Sharpe Ratio example is reproduced in VAL-002.

6. **PSR and DSR (VAL-002).** `xq.validation.dsr` implements the probabilistic Sharpe ratio, the
   expected maximum Sharpe ratio of N trials under the null (Euler–Mascheroni approximation), and
   the deflated Sharpe ratio. Trials record **annualized** Sharpe ratios of daily net returns;
   their variance is divided by `periods_per_year` for per-period units.
   `deflated_sharpe_for_family` uses the family's **effective** trial count (ADR 0023, clustering
   near-duplicates) by default, and `use_effective=False` gives the raw count. Reports show both.
   With fewer than two trials the benchmark is 0.
7. **DSR verification.** The published example (Bailey & López de Prado 2014: annualized SR 2.5,
   T = 1250, N = 100, V = 0.5, skewness −3, kurtosis 10) is reproduced: expected maximum 0.1132,
   DSR 0.9004. On simulated pure-noise families of 20 strategies, the best strategy passes
   DSR > 0.95 in at most 8 % of families, where the undeflated PSR passes in more than 30 %.

8. **Forecast comparison (VAL-005)** in `xq.validation.forecast_eval`, on per-observation loss
   series (BASE-006):
   - Diebold–Mariano uses the autocovariances up to `horizon − 1`, falling back to Newey–West
     weights if that is not positive, with the Harvey–Leybourne–Newbold correction and Student's
     t(T − 1).
   - Giacomini–White uses instruments lagged by the horizon (default: a constant and the lagged
     loss difference), with a Newey–West Ω and a χ²(q) test.
   - The Model Confidence Set (Hansen–Lunde–Nason) uses T_max, sequential elimination and the
     stationary bootstrap. Its p-values never decrease along the eliminations, and the set is the
     models with p ≥ α (default 0.10).

   Verification: hand-computed statistics; size on simulated nulls (DM at horizons 1 and 4, and
   GW, reject 2–10 % at the 5 % level); power on alternatives (DM on a better forecast; GW on
   predictable loss differences with zero mean); the MCS keeps all equal models in at least 85 %
   of simulations at α = 0.10 and drops a clearly worse one.

## Consequences

- A Sharpe ratio is never reported without an interval. The i.i.d. error understates the
  uncertainty of autocorrelated or skewed strategy returns, and the tests demonstrate that.
