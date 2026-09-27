# ADR 0056 — Monte Carlo with the risk engine, noise injection, the robustness report and `xq validate-strategy` (Sprint 12 remainder)

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** Claude, within the plan (Phases 14, 16 and 17) and the owner's instruction after
  the Sprint 9 review. Open points are flagged for the owner's review.
- **Tasks:** ROB-004, ROB-005, ROB-008, and `xq validate-strategy` (Phase 17's acceptance)

Everything here runs on synthetic data and simulated strategies only. Nothing has run on real
data.

## ROB-004 — Monte Carlo equity with the risk rules applied

1. **The real risk engine, not a model of it.** The resampled paths go through
   `RiskEngine.evaluate` and `RiskStateTracker`, the code the event backtester uses. Sizing, the
   drawdown throttle, the daily-loss, drawdown and cooldown halts, the caps and the stop policy all
   apply as they would in a backtest. The profile is the configured one (`risk-2`, provisional).
2. **Trade outcomes in risk units.** Each closed trade becomes an R-multiple: its net return over
   the notional at entry, divided by a stop at `stop_sigmas` (3) daily sigma-hats, the event tier's
   default. Costs are therefore inside R. R is taken as observed: a loss beyond the stop (a gap, or
   a strategy that places no stops) keeps its size. That is the conservative reading.
3. **Paths.**
   - The R-multiples are resampled with a stationary bootstrap (a Politis–White mean block of at
     least one trade), which keeps clusters of losses.
   - The calendar of the trades (entry and exit times, direction, price, spread, sigma-hat) is
     kept. Daily limits, cooldowns and the trades-per-day cap therefore meet the timing the
     strategy really had.
   - Each approved entry requests the most exposure the profile allows, so the risk budget sizes
     it.
   - A refused entry (a halt, a cooldown, a rejected stop, or a size rounded to zero) skips the
     trade.
4. **Reported per path:**
   - the maximum drawdown, with the capital as the first peak (ADR 0055);
   - the final equity;
   - whether the drawdown halt fired;
   - whether the equity fell to `ruin_level` (0.5, provisional) of the capital;
   - entries taken and refused.

   From these: the drawdown quantiles, the probability of hitting the halt level and the ruin
   probability.
5. **The R2 gate** `monte_carlo_drawdown` reads the 95 % quantile of the maximum drawdowns
   against the 15 % halt level (strictly below).
6. **Known truth.**
   - With only the risk budget binding (no throttle, halts or caps), every path equals the
     fixed-fractional recursion `E(k+1) = E(k) (1 + 0.5 % R(k))` within lot rounding, on 10
     million USD.
   - A strategy losing 0.1 R a trade after costs (300 trades, 100 paths):
     - under the default profile its 95th-percentile drawdown stays below the halt and the gate
       passes, with no halt and no ruin (the throttle shrinks the size to zero lots first);
     - with the throttle and halts turned off, the same outcomes pass 20 % and fail;
     - a reckless profile (3 % a trade, no throttle, the default halt) hits the halt on more than
       90 % of paths and overshoots it, so the gate fails;
     - without the halt, the reckless profile ruins most paths.
   - Hourly trades that all lose meet the cooldown: five are taken, the next seven are refused.
7. **Speed.** About 0.3 ms a replayed trade, in pure Python through the engine: 1,000 paths of 300
   trades take about a minute and a half. `n_paths` (1,000) is in `config/validation.yaml`.

## ROB-005 — noise injection

1. **Noise goes into the inputs, never the fills.** The strategy sees disturbed prices or
   features. Its fills, costs and P&L stay on the true prices, so the curve measures how much of
   the edge depends on the inputs being exact.
2. **Price noise** is Gaussian with a standard deviation of `level` times the spread at each
   instant, independent per price column. The levels are 0.25, 0.5, 1, 2 and 5 spreads.
3. **Feature noise** is Gaussian with a standard deviation of `level` times the feature's
   expanding standard deviation over the rows **before** it, so no row's noise depends on later
   data. The first 20 values are left as they are. The levels are 0.1, 0.25, 0.5 and 1 sigma.
   A full-sample standard deviation would be harmless here, since it only scales the noise, but
   the causal one keeps the code within the invariants without an exception.
4. **The degradation curve.**
   - Level 0 is the undisturbed strategy.
   - Every other level is drawn 20 times with derived seeds and summarized by its median, a 90 %
     band and the retention (median over the undisturbed Sharpe ratio).
   - The breakdown level is the first level with a non-positive median.
   - ROB-005 is P2 in the plan and has no R2 gate: the curve is reported, not gated.
5. **Known truth.**
   - Price noise has a standard deviation of `level` times the spread, per row and independent
     per column.
   - Feature noise scales with each feature's own sigma and does not change when later rows
     change.
   - A bid-ask-bounce edge (fading the last tick of mids that bounce by half a spread) keeps less
     than half its Sharpe ratio at one spread of noise and less than 15 % at five.
   - A trend edge on daily drift keeps more than 80 % at five spreads.
   - Under feature noise on its t-statistic the trend edge degrades without a cliff, keeping more
     than 70 % at 0.25 sigma and more than 30 % at one sigma.

## ROB-008 — the robustness report and score

1. **One subject interface.** A `StrategySubject` carries what the reports need about a strategy:
   - its daily net returns, closed trades and walk-forward folds;
   - the family of configurations it was selected from;
   - its trial counts;
   - functions that re-evaluate it at another parameter point, under stressed costs, with
     delayed orders or with noisy inputs;
   - the slices its hypothesis declared.

   Adapters build it for each kind of source, so every strategy is judged by the same code
   against the same gates.
2. **What is judged.** Seven R2 gates are robustness gates:
   - `parameter_neighbourhood`: the full grid (ADR 0055);
   - `stressed_costs`;
   - `monte_carlo_drawdown`;
   - `max_single_year_pnl_share`;
   - `execution_delay`;
   - `positive_folds_share_min`: walk-forward test folds with positive net P&L;
   - `oos_max_drawdown_max`: the evaluated period's drawdown, capital as the first peak.

   Reported without a gate: the bootstrap intervals and trade permutation (ROB-003), the noise
   curves (ROB-005), the slice tables (ROB-006; volatility terciles "descriptive, cut ex post")
   and the one-at-a-time sensitivity table.
3. **Score and verdict.**
   - The **robustness score** is the share of the evaluated robustness gates that pass.
   - The **verdict** is `pass` only when all seven are evaluated and pass, and `fail` when any
     fails.
   - It is `incomplete` when none fails but one could not be evaluated, with the reason: a
     strategy without tunable parameters has no neighbourhood, and one without closed trades has
     nothing to resample. A gate is never passed by default.
   - **Open point for the owner:** a strategy with no tunable parameters (buy and hold, or a
     forecast-sign strategy with fixed models) therefore never reaches `pass`. The alternative is
     to treat its neighbourhood as vacuously satisfied.
4. **The plan's `robustness_results` rows.** Every measure is also a `RobustnessResult` (test id,
   parameters, metrics, pass or None), stored by `xq validate-strategy`.
5. **Settings.** Every measure's settings are in `config/validation.yaml`: perturbation levels,
   Monte Carlo paths, noise levels and draws. The bootstrap uses the gates' convention (10,000
   resamples, Politis–White, at least 5 days).

## Simulated strategies with known truth (`xq.robustness.simulated`)

The reports must tell a genuine edge from an overfit one before any real candidate meets them.
The simulations of ADR 0054 were helpers in the tests and produced returns only. The reports
need strategies they can re-evaluate: at another parameter point, with its costs stressed,
delayed or with noisy inputs. Two such strategies now live in the library, marked synthetic
everywhere they appear.

- **Market.** Each trades one synthetic asset on 20 years of daily bars, with positions of −1, 0
  or +1 times the capital. Costs per unit of turnover are half a 1.5 bp spread, 0.5 bp of
  slippage and 0.35 bp of commission, plus 0.15 bp a day of financing on the position.
- **genuine.** Returns carry a persistent drift: an AR(1) mean with persistence 0.99 and a
  standard deviation of 6 bp, plus 40 bp of daily noise. The strategy is a trend rule over 6
  lookbacks times 3 deadbands, and the candidate is the in-sample best of the 18. Its net Sharpe
  ratio is typically 1.0–1.7, a plausible edge and far below the "too good" line of CLAUDE.md.
- **overfit.** Returns are pure noise. Every one of 50 parameter points holds random ±1 spells
  from a generator seeded by the point, so each configuration is independent noise. The candidate
  is the in-sample best: a single-point optimum on noise.
- A `SimulationSpec` (truth, seed, length, costs) defines each strategy completely, so a
  simulated run can be rebuilt exactly from its recorded configuration.

**Known truth.**

- For seeds 0, 1 and 2 (100 Monte Carlo paths and 3 noise draws in the test, for speed), the
  genuine edge passes all seven robustness gates: score 1.0, verdict `pass`.
- For the same seeds the overfit strategy fails, always on the neighbourhood gate.
- Over seeds 0–5 during development, every genuine seed passed and every overfit seed failed.
- Removing the genuine strategy's parameters makes the verdict `incomplete`, with a score of 1.0
  over six gates.
- The subjects rebuild identically from their spec. The candidate is the family's best. The
  re-evaluation functions reproduce the recorded returns at level 0, delay 0 and the nominal
  parameters. The overfit configurations are uncorrelated.
