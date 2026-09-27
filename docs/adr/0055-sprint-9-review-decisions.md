# ADR 0055 — Sprint 9 review decisions (C-24)

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, at the Sprint 9 review (C-24); amends ADR 0054
- **Tasks:** VAL-004, BT-003, ROB-001, ROB-006, EXP-006 (and ROB-008, `xq validate-strategy`,
  which read them)

## Context

ADR 0054 left eight open points for the owner (C-24). The owner decided five of them and approved
the rest as they stand.

**Approved as they stand:**

- cost stress runs the plan's grid one dimension at a time plus the gate's combined scenario (no
  full factorial);
- a financing credit is divided by the stress multiplier (a stress always costs more);
- the execution delay shifts entries and exits alike;
- a reproduction adds no trials;
- the deflated Sharpe ratio of a reproduction is shown, not judged.

The five decisions follow, each with how it is implemented and what it showed.

## 1. SPA and the Reality Check use the gates' bootstrap convention, and warn when they over-reject

**Decision.** SPA and the Reality Check take their block length from the gates' bootstrap
convention (`conventions.bootstrap` in `config/gates.yaml`: Politis–White, at least 5 days)
instead of any fixed block. The size simulation is re-run under it. If the rejection rate is still
above 1.5 times nominal, every gate result that uses SPA or the Reality Check carries the warning
"test over-rejects on this sample".

**Implementation.**

- `family_tests` takes the gates' `GateBootstrapConfig` and has no block argument, so a caller
  cannot choose a block. Its `n_boot` defaults to the convention's (10,000).
- `size_check` measures the size on the sample at hand. It fits an AR sieve to each strategy's
  demeaned differentials: Yule–Walker, order 0–5 by AIC, always stationary. It then simulates
  null families with those dynamics. The residual rows are resampled together, so dependence
  across strategies is kept, and every true mean is zero, the least favourable null. It runs the
  tests on each family under the same convention and counts rejections at the gate's level.
- `FamilyTest.gate_check(gates, size)` gives the R2 `spa_p_max` result. The size check is a
  required argument, so no SPA gate result exists without one.
- The warning is attached when the simulated size exceeds `warn_ratio` (1.5) times the level. The
  Reality Check, which is reported but not gated, gets the same warning from the same check.
- A warning never changes a result: thresholds stay fixed. `GateCheck` now carries `warnings`, and
  `describe()` prints them.
- The simulation settings are in the new `config/validation.yaml` (`spa_size_check`: 500
  families, 199 resamples each, AR order at most 5, ratio 1.5).

**The re-run size simulation.** These are noise-only families of AR(1) strategies with φ = 0.4
and 400 periods, as in ADR 0054. They use the gates' convention, 199 resamples per test and a
level of 10 %. The median Politis–White block was 5.9 days. Rejection rates:

| Strategies | Replications | Reality Check | SPA | Romano–Wolf (any) |
| --- | --- | --- | --- | --- |
| 1 | 2,000 | 12.4 % | 12.4 % | 12.4 % |
| 4 | 2,000 | 13.6 % | 15.0 % | 14.6 % |
| 8 | 2,000 | 15.4 % | 18.5 % | 18.0 % |
| 20 | 2,000 | 17.2 % | 23.0 % | 22.5 % |

For comparison, the same 8-strategy family with fixed blocks:

| Fixed block | Reality Check | SPA |
| --- | --- | --- |
| 5 | 16.5 % | 19.6 % |
| 10 | 14.3 % | 18.3 % |
| 20 | 14.4 % | 21.0 % |

With 8 strategies and 1,000 replications:

| Sample | Reality Check | SPA |
| --- | --- | --- |
| iid, 400 periods | 9.7 % | 11.8 % |
| φ = 0.2, 400 periods | 12.1 % | 14.4 % |

The over-rejection grows with the family's size and the strength of the dependence. It is **still
above 1.5 times nominal** (15 %) for SPA at 8 or more strongly dependent strategies, so the warning
applies.

The warning is decided per sample, by the size check, rather than attached to every result
whatever its data. On iid-like samples the tests are close to nominal (the table above and ADR
0054), and a blanket warning there would be false. The check itself is proven in both directions:

- On an AR(1) φ = 0.4 sample (400 periods, 8 strategies) the sieve finds the dependence, the
  simulated SPA size exceeds 15 % and the gate result carries the warning.
- On an iid sample of the same shape neither test is flagged.

**Cost.** One check on 36 strategies over 1,000 days takes about 20 s.

## 2. BT-003 drawdowns start from the capital

**Decision.** In BT-003's metrics the starting capital is the first equity peak, as ROB-003's
bootstrap already did. This was the queued task from ADR 0054 (ROB-003, item 2).

**Implementation.**

- `drawdown_metrics(equity, capital)` takes the capital as a required argument, so no caller can
  forget it. The running peak is `max(capital, running maximum of equity)`, so a loss on the first
  day is already a drawdown, and its days count towards `max_drawdown_days`.
- `running_peak` and `path_max_drawdowns` in `xq.backtest.metrics` are the one definition. They
  are shared by `performance_metrics` (both backtest tiers), the board's `max_drawdown` and its
  bootstrap interval, ROB-003's resampled and permuted paths, and the drawdown panel of the
  backtest report.

**Affected results.** Equity 99, 98, 97 on a capital of 100 now reports 3/100 over three days,
where it reported 2/99 over two before. A path that first gains is unchanged: the capital is a
floor on the peak, not a ceiling.

The only expectation that changed is the independent pandas check in `test_metrics.py`: its
reference peak is now floored at the capital. The number was never wrong by the old definition;
the definition changed. The R2 gate `oos_max_drawdown_max` reads this metric, so a strategy that
loses from its first day can now fail it where it passed before. No gate result exists yet.
