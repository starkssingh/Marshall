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

## ROB-002 — cost and latency stress

1. **One dimension at a time, plus the gate's scenario.** The plan's grid is run one dimension at
   a time: spread x1.25, 1.5 and 2; slippage x2 and 3; latency +250 ms, +1 s and +5 s; financing
   x1.5. The R2 scenario (`stressed_costs`: 1.5x spread and 2x slippage together) is always run,
   whatever scenarios are asked for, and its net Sharpe ratio is the gate's measure (> 0). A full
   factorial grid is left to the robustness report if the owner wants it.
2. **How each cost is stressed.** A wider spread widens every quote around its mid, so the fills
   stay at the correct side of real quotes and the fill times and mids (and so the lot sizes)
   are unchanged. Slippage multiplies the model's fixed and sigma terms; the session and event
   multipliers stay on top. A financing stress multiplies a charged rate and divides a credited
   one, so the stress always costs more. Latency is added to the model's own (1 s for the
   placeholder).
3. **Break-even multiplier.** The multiplier k of every cost at which net P&L is zero. Net P&L is
   almost, but not exactly, linear in k, because slippage is charged on the widened side price.
   So k is found by the secant method on actual runs, from the linear estimate
   `gross / costs`, to 1e-9 of the costs. It is 0 for a strategy that loses before costs and
   infinite for one that pays none. Latency stays at the model's own.
4. **Known truth.** In synthetic markets with a planted drift or jump (edges known by
   construction, so their large Sharpe ratios test mechanics, not a strategy): the R2 scenario
   passes exactly when the gross P&L exceeds the stressed costs; a thin edge that is profitable
   at baseline fails; a run at the break-even multiplier nets zero; a doubled spread doubles the
   spread cost exactly with unchanged fill times; and when a signal is priced in linearly over
   10 s, the gross P&L falls to (10 − latency)/(10 − 1) of the baseline's.

## ROB-003 — block bootstrap and trade-order permutation

1. **Return intervals.** Daily net returns are resampled with the stationary bootstrap. The
   mean block is Politis–White, at least 5 days (the gates' convention, shared with VAL-001 and
   VAL-004). The intervals are percentile intervals of the annualized Sharpe ratio, the CAGR and
   the maximum drawdown. CAGR follows `performance_metrics`: the screener sizes on constant
   capital, so equity is capital plus cumulative P&L. `n_boot` is passed in; the robustness
   report (ROB-008) uses the gates' 10,000.
2. **Drawdowns start from the capital.** A loss on the first day is already a drawdown. BT-003's
   `drawdown_metrics` takes its first peak at the first day's equity, so it understates a
   drawdown that begins on day 1 (equity 99, 98, 97 on 100 reports 2/99, not 3/100). That is
   recorded as a known issue with a follow-up fix. Here the capital is the first peak.
3. **Trade order.** Closed trades' net P&L is shuffled. The total is unchanged; the path is not.
   The maximum drawdown and the longest run of trades below the running peak are reported over
   the orders, with the observed order's percentile: near 1, the actual sequence clustered its
   losses. It is counted in trades, not days, because shuffling has no calendar.
4. **Known truth.** With 200 replications and 90 % intervals:
   - Sharpe and CAGR coverage lies within 0.85–0.95 for iid and GARCH returns, and for AR(1)
     returns (φ = 0.3) with Politis–White blocks. The iid bootstrap (block 1) on the same AR(1)
     returns under-covers at about 0.79.
   - The drawdown interval covers the true median drawdown (from 5,000 fresh paths) in
     0.85–0.99 of replications, and the bootstrap median is within 15 % of it.
   - Percentiles of unordered trades are uniform (5 % above 0.95, 5 % below 0.05); losses sorted
     first sit at percentile 1.
5. **Limitation.** The drawdown interval is an interval for the drawdown of this sample's return
   process, not a prediction interval for the next path. A fresh path's drawdown falls outside
   the 90 % interval about 22 % of the time, because it has its own mean. The distribution of
   future drawdowns under the risk rules is ROB-004's Monte Carlo.

## ROB-006 — pre-registered slicing

1. **Slices come from the locked hypothesis, never from the caller.** `run_slices(engine,
   run_id)` reads the `slices` field from the registered text of the hypothesis version the
   run's experiment tests. An edit registered after the run creates a new version and does not
   change what that run is sliced by. `DeclaredSlices` refuses construction outside the loaders,
   the same pattern as `OrderIntent`, so a report cannot slice by whatever looks best after the
   fact. Slices are descriptive: reported, not tested, with no p-values and no trials.
2. **Vocabulary.** Names are compared in lower case with spaces, hyphens and slashes read as
   underscores.
   - `year`: the calendar year of the trading day.
   - `volatility_tercile`: low, mid or high by the 1/3 and 2/3 quantiles of the sliced days'
     daily sigma-hat, where each day's value is the one known at its start. The cut points use
     the whole sliced period. That is an after-the-fact grouping for a report and never feeds a
     decision, so it is not a full-sample normalization of a feature.
   - `session`: closed trades by entry session from `config/sessions.yaml`, converted to UTC per
     date. A configured overlap is named when a trade lies in exactly its sessions; any other
     combination is joined with `+`; `off_session` otherwise.
   - A name outside the vocabulary is refused when loaded. A regime slice (any name containing
     "regime") is a legitimate declaration, refused only when computed until REG-007 provides a
     causal regime model. The template's `volatility regime` became `volatility tercile`, which
     is what the plan's ROB-006 lists.
3. **The single-year gate is always computed.** R2's `max_single_year_pnl_share` (at most 0.50)
   is a pre-registered gate in `config/gates.yaml`, so it does not depend on the hypothesis
   declaring a year slice. It is NaN, and so fails, unless total net P&L is positive.
4. **Known truth.** On planted edges:
   - P&L earned only in 2022 gives that year more than 80 % of the total and fails the gate; a
     steady edge gives each of four years about a quarter and passes.
   - An edge only on high-sigma days puts more than 80 % of the P&L in the high tercile, with
     the days split in thirds.
   - A 12:30 UTC entry is London-only in January and in the London–New York overlap in July.
   - A screened backtest's trades and days add up across the slices to its totals.
5. **Open point for the owner.** Slice names are checked when the slices are loaded, not when
   the hypothesis is registered. A typo therefore surfaces only at report time and needs a new
   hypothesis version. Checking at registration would move the vocabulary into the tracking
   layer.

## ROB-007 — execution-delay sensitivity

1. **What is delayed.** Every order, entries and exits alike, comes 1, 2 or 3 decision bars late.
   The target series is shifted by k bars on its own decision grid and is flat before the
   first. Fills, costs and latency are otherwise unchanged, so the curve isolates the timing of
   the information. The plan's wording is "entries late". Delaying only entries would also
   change holding periods, which would mix timing with a different strategy.
2. **Reported.** The net Sharpe ratio at each delay; its retention (over the undelayed Sharpe,
   NaN unless that is positive); and whether the curve flips (a positive undelayed Sharpe that
   turns negative). The R2 gate reads only the delay of `execution_delay.bars` (1): net Sharpe
   > 0. Smoothness is reported, not gated.
3. **Known truth.**
   - A trend rule on a drift with persistence 0.97 has a median undelayed Sharpe of about 1.6.
     Over 40 seeds its median retention falls steadily, to above 0.8 at three bars; at most two
     seeds flip, and at least 95 % pass the gate.
   - A reversal rule on a bid-ask bounce (AR(1), φ = −0.3) flips at the first delay in every
     seed and fails the gate.
   - A strategy trading on the return it earns (a look-ahead leak) has an undelayed Sharpe above
     10, far too good and the tell of CLAUDE.md, and keeps under 2 % of it one bar later.
   - Per seed, a delayed Sharpe ratio can tick up by chance, so smoothness is judged on the
     median across replications, as a report on one strategy should judge it against its own
     bootstrap interval (ROB-003).

## EXP-006 — `xq exp reproduce <run_id>`

1. **Rebuild, rerun, compare.** The dataset is rebuilt from the resolved spec that
   `dataset_versions` recorded, so it can be rebuilt even if its directory is gone. The builder
   already refuses different content for the same id. A spec that now builds another id means the
   producing code changed, and the reproduction fails. The run is then repeated in a new run
   context of kind `reproduction`, under the original's hypothesis, with its run configuration and
   seed. The walk-forward prediction cache is off, so every forecast is recomputed. Finally every
   metric the original logged is compared with `|b − a| ≤ atol + rtol·|a|`, defaults 1e-9 and
   1e-6. A rerun on the same code and data is exact; the tolerance only allows for floating-point
   differences between machines.
2. **A reproduction adds no trials.** The same configuration on the same data is not a new
   trial. `RunContext.reproduces` makes `record_trial` return the original run's trial for a
   configuration it already recorded in that family. A configuration the original never
   evaluated is still counted. Without this, every reproduction would inflate the family's raw
   trial count and flag it for review.
3. **Registry-dependent metrics are shown, not judged.** The board's deflated Sharpe ratio uses
   the family's trial count at the time, so it can move when other runs add trials, even though
   the run itself reproduces. Metrics ending in `/dsr` are listed but not judged.
4. **Provenance differences are reported, not refused.** Git sha, `uv.lock` hash and the
   application config hash are compared and each difference is printed: code moving on is what
   a reproduction tests. A confirmatory reproduction still needs a clean tree (`--exploratory`
   otherwise).
5. **Scope.** Reproducers exist for `baseline_board` runs, the only kind the CLI can start with
   a dataset today. Other kinds are refused by name until they get one; that is an open point for
   the robustness report (ROB-008) and for `xq validate-strategy`. The reproduction run sits in
   the hypothesis's open experiment, so the sprint-end audit (`xq exp audit`) sees it with the
   original.
6. **Known truth.** A fixture board run (synthetic ticks, the fixture board) reproduces with every
   judged metric equal within 1e-9 and the family's trial count unchanged. With one stored metric
   altered by 0.1, exactly that metric is reported as a mismatch and the command exits 1. Altered
   dataset bytes are refused (exit 2). An unfinished run and a kind without a reproducer are
   refused.
