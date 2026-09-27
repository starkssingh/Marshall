# ADR 0031 — Statistical validation: Sharpe inference, PSR/DSR, forecast comparison

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** VAL-001 (VAL-002 and VAL-005 add sections below)

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

## Consequences

- A Sharpe ratio is never reported without an interval. The i.i.d. error understates the
  uncertainty of autocorrelated or skewed strategy returns, and the tests demonstrate that.
