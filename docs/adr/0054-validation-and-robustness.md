# ADR 0054 — Statistical validation and robustness (Sprint 9)

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** Claude, within the plan (Phases 16, 17 and 18) and the owner's Sprint 9
  instructions (ADR 0051, the Sprint 12 A review); open points are flagged for the owner's review
- **Tasks:** VAL-003, VAL-004, VAL-006, ROB-001, ROB-002, ROB-003, ROB-006, ROB-007, EXP-006

## Validation on known truth

Every method is proven on simulated strategies whose answer is known before the method runs, and
the proving tests are listed in the recovery registry (`xq.research.recovery.RECOVERY_TESTS`,
ADR 0043) next to the Sprint 6 methods. A method without an entry must not be used. The
simulations (`tests/helpers/strategies.py`) are:

- a **noise-only family** (no configuration has any edge);
- a **graded family** (true means rising with the configuration);
- a **single-point optimum on noise** (a two-parameter strategy whose returns at every point are
  independent noise, so the in-sample winner is a fluke);
- a **genuine trend edge** (returns with a slowly varying drift, and a trend rule).

## VAL-003 — probability of backtest overfitting (CSCV)

1. **The configuration matrix** holds the per-period net returns of every configuration tried on
   the same periods. It is cut into S contiguous blocks (default 16, even; a remainder of fewer
   than S periods at the end is dropped), and every one of the C(S, S/2) halves is used in turn as
   in-sample.
2. **Performance** is the per-period Sharpe ratio (or the mean). The in-sample winner's relative
   out-of-sample rank uses average ranks for ties, and PBO is the share of logits **at or below
   zero** — the winner at or below the out-of-sample median counts as overfit. This is the
   conservative reading of the paper's integral up to zero.
3. **Known truth.** A single noise family's PBO scatters widely (0.35–0.67 in the tests), as the
   theory implies. The average over 30 noise families is 0.5 ± 0.06, and more than 90 % of them
   exceed the R2 limit of 0.20. A graded genuine edge has PBO ≤ 0.20 with a probability of loss
   below 5 %. The single-point optimum on noise exceeds 0.20. The degradation slope is reported
   but not tested: the two halves are complementary, so it tends to be negative even for a
   genuine edge.

## VAL-004 — Reality Check, SPA and Romano–Wolf

1. **Differentials against a benchmark.** The input is the per-period net returns of every
   strategy in the tested family minus the benchmark's (cash, zero, unless a baseline is given).
   The family-wide null is that none beats the benchmark.
2. **One bootstrap for all tests.** A stationary bootstrap of the periods, with the same indices
   for every strategy. The mean block length follows the gates' convention: Politis–White on the
   family's average differential, at least 5 periods. Studentization uses each strategy's
   bootstrap standard deviation (Hansen). p-values are `(1 + #{bootstrap ≥ observed}) / (1 + B)`.
3. **The R2 gate reads Hansen's consistent SPA p-value.** The lower and upper bounds and White's
   Reality Check are reported with it. Romano–Wolf adjusted p-values (step-down, made monotone)
   name the **survivors** at a level.
4. **Known truth and a limitation.** On noise-only families the rejection rates at 10 % are close
   to nominal: in 1,500-replication checks, iid and GARCH, the Reality Check rejects at 9.8–10.3 %,
   SPA at 11–12 % (slightly liberal, as is known for it) and Romano–Wolf at 10.6–11.7 %. The unit
   tests use smaller replications with wider bands. Strongly autocorrelated returns in short
   samples over-reject: with AR(1) φ = 0.4 and 400 periods, the Reality Check rejects at 15 % and
   SPA at 20 %, even with longer blocks. Daily strategy returns are usually far less
   autocorrelated, but a family with strong serial dependence (for example, overlapping holding
   periods) should be tested on non-overlapping periods. Recorded as a known issue.

## VAL-006 — multiple-testing control per test family

1. **Holm by default, Benjamini–Hochberg only where a family declares it.** A family's tests are
   adjusted together and never with another family's. The default controls the family-wise error,
   because a gate's claim ("this strategy works") is costly when false. False-discovery control
   is for screening families that feed follow-up work, never for a gate.
2. **One implementation.** `xq.validation.multiple_testing` now holds Holm, Benjamini–Hochberg and
   Bonferroni. The Sprint 6 helper `holm_adjust` delegates to it, so the statistical studies and
   the volatility selection use the same code. Missing p-values stay missing and do not count.
3. **Known truth.** Hand-computed reference values, agreement with statsmodels to 1e-12, and on
   simulated nulls the family-wise error (Holm) and the false discovery rate (BH) at or below the
   level. Recording adjusted p-values in a `stat_tests` table waits for the validation report
   (`xq validate-strategy`), which the plan attaches to Phase 17's report, not to VAL-006.

## ROB-001 — parameter perturbation and plateau metrics

1. **Values.** At each level (10, 20 and 30 %) a parameter moves to `nominal ± level × scale`,
   where scale is |nominal| unless the parameter declares one (a zero nominal must). An integer
   parameter moves to the nearest integer (halves round up), and always at least one step, so a
   short lookback is still perturbed. A parameter with a grid of allowed values moves to the
   allowed value nearest the target on each side: the plan's "neighbouring discrete values".
   Values below a declared minimum are dropped, not clipped, so no point is counted twice.
2. **Designs.** One at a time; jointly (every combination of {down, nominal, up}, the nominal
   excluded, 3^k − 1 points for k parameters); and for heat maps every pair of parameters over all
   levels' values, the others at nominal. Each distinct point is evaluated once. The heat maps
   are returned as tables; drawing them belongs to the robustness report (ROB-008).
3. **The gate reads the joint neighbourhood.** R2's `parameter_neighbourhood` compares the share
   of the joint ±20 % neighbourhood with net Sharpe > 0 against 0.70, with the boundary rule of
   `GatesConfig.criteria` (at least). A point without variance, such as one that never trades, is
   not profitable. The joint design is the stricter reading: a strategy can be flat in each
   parameter alone and still fall apart when two move together. The median-to-nominal ratio is
   reported with it; it is not gated.
4. **Known truth.** The strategy is chosen in sample in both simulations, as a real one would be.
   A single-point optimum on noise (the best of a 10 × 5 grid of pure-noise points) has eight
   fresh-noise neighbours: it passes only when at least six of eight are positive, 37/256 =
   14.5 %. Over 300 replications 84 % failed, and the test asserts that at most 25 % of 100 pass.
   A trend rule (lookback and a t-statistic deadband) on returns with a persistent drift, best of
   an 18-point grid, passes in 98 % of 60 replications (asserted at 90 %), with a median
   median-to-nominal ratio above 0.7. The genuine edge's annualized Sharpe is about 0.9: a
   plausible edge, not a leak.
