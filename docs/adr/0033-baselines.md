# ADR 0033 — Baselines: forecast baselines, rule strategies and the random-entry null

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** BASE-001, BASE-002 (used by BASE-005)

## Context

Phase 10 asks for a benchmark board that every candidate must beat on identical folds, costs and
metrics. BASE-001: zero return, random walk, expanding historical mean and training-fold class
frequency (climatology). BASE-002: rule strategies with parameters fixed in advance — buy-and-hold
with financing, random entry matched to the candidate's trade count and holding-time distribution
(1,000 seeds as a null distribution), time-series momentum, z-score mean reversion, MA crossovers
(20/50, 50/200), Donchian/ATR breakout and volatility-targeted versions of each. Failure
condition: tuning baseline parameters. All code in `src/xq/models/baselines.py` (plan layout).

## Decision

1. **Forecast baselines** are `ModelSpec`s for the walk-forward runner, without grids:
   - `zero_return` forecasts 0, the random walk's forecast of a log return;
   - `random_walk` is the random walk applied to the target series itself (a persistence
     forecast): the latest realized value of the forecast quantity known at the decision time,
     the log return of the latest completed bar of the horizon's timeframe (`ctx_<h>_open/close`,
     joined on availability; the decision bar when the horizon is the base timeframe). Without
     this reading it would duplicate `zero_return`. It is a regression baseline for raw return
     targets only;
   - `historical_mean` is the mean of the fold's training targets — with expanding windows the
     expanding mean at the forecast origin (a random walk with drift), held over the test fold;
   - `climatology` is the training fold's frequency of positive targets, as a probability.
2. **Rule strategies run on signal bars**: the distinct context bars a dataset carries, indexed
   by their availability (`signal_bars`, from `ctx_<tf>_*` and `ctx_<tf>_available_at`). The
   classic parameters are daily, so the board uses the `1d` context bars: a signal changes only
   when a new daily bar becomes available (17:00 New York, while the market is closed, so it is
   traded at the first decision after the reopen, ADR 0032). `positions_at` places each signal at
   decision times with an as-of join on availability.
3. **Rule definitions** (module docstring, exact): momentum is the sign of the lookback log
   return; z-score reversion fades `|z| >= entry` and exits at `exit`; the MA crossover is long
   above and short below (always in the market once both averages exist); the Donchian breakout
   enters on a break of the prior `entry`-bar channel and exits on the prior `exit`-bar channel or
   a stop fixed at entry `atr_stop` simple-mean ATRs away, with same-bar reversal. Volatility
   targeting scales exposure by `min(max_exposure, annual_vol / realized)` with realized
   volatility from the last `lookback` daily log returns, and holds no position until it is known.
   Exposures are then screened by BT-002 (fills, costs, financing), never sized by a model.
4. **Parameters are fixed in the board configuration** (`experiments/configs/baselines/`), from
   the literature's standard choices, and never tuned.
5. **Random-entry null.** A template position series' holding episodes (maximal runs of non-zero
   exposure of one sign, with their exposure paths) are placed at random on the same decision
   grid. This matches the trade count, the holding times (in decision steps, which are trading
   time) and the long/short mix exactly; only the timing is random. The order of episodes is a
   uniformly random permutation among those that fit (same-side neighbours need a flat decision
   between them); an always-in-the-market template, where nothing fits, keeps its side sequence
   and permutes holding times within each side. Leftover flat decisions are split uniformly over
   all compositions. Seeds are `derive_seed(base, "random_entry", i)`. Null draws are a reference
   distribution, not configurations being selected, so they are not recorded as trials.
6. **Leakage.** Rule positions pass the DS-006 harness (truncation, perturbation, availability)
   for every rule and its volatility-targeted version, and the harness catches a planted leak
   (signals placed at their bar's start instead of its availability).

## Consequences

- A strategy candidate's timing skill can be tested against its own random-entry null.
- Daily-bar rules trade at most once a day, right after the reopen, where the rollover spread
  window's slippage multiplier applies: their costs are realistic, not flattering.
- The random walk baseline needs the horizon's context timeframe in the dataset (`1h`, `4h`,
  `1d` in `ds_base.yaml`).
