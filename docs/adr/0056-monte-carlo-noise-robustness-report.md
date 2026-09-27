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
