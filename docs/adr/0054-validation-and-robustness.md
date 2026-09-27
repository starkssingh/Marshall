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
