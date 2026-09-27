# ADR 0034 — The baseline board runner

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** BASE-005 (on BASE-001, BASE-002, BT-001–003, VAL-001/002/005, VAL-007)

## Context

BASE-005: all baselines through walk-forward and the standard cost model, stored per target,
horizon and timeframe; `xq baselines run --dataset <id> --target <name>`. Acceptance: the board
regenerates in one command; all metrics include bootstrap CIs. Sprint 4's working system: a
walk-forward, net-of-cost baseline board with Sharpe CIs and DSR using the registered trial count,
recorded as experiment H-0001. Owner decisions in force: evidence conventions of
`config/gates.yaml` and "screening, placeholder costs" on every net result (ADR 0032).

## Decision

1. **Layout.** The runner is `xq.models.board` — a module the plan's section 3 does not list;
   it sits beside `baselines.py` because it orchestrates only baselines. The CLI group lives in
   `xq.cli.main` like the existing groups. The board configuration is
   `experiments/configs/baselines/board.yaml`; its parameters are fixed in advance and hashed
   (`BoardConfig.config_hash`) into every trial.
2. **Identical folds.** One walk-forward splitter (the Phase 11 defaults: expanding, at least 3
   years of training, 91-day test folds, 1-day embargo, no validation window) defines the folds for
   every target and strategy. Rule strategies are judged on the union of the test windows.
3. **Forecast baselines** go through `run_walk_forward` per target (`record_trial=False`); the
   board reports their losses with a bootstrap interval of the mean loss and a one-sided
   Diebold–Mariano p-value of beating `zero_return` (horizon in base bars). Each one except
   `zero_return` (always flat) becomes a strategy that holds the sign of its forecast.
4. **Screening.** Every strategy is screened by BT-002 with the configured cost model on the
   dataset source's usable quotes (`usable_quotes`, shared with the target builder: ticks with a
   bar-excluded flag or on excluded days are dropped; the catalog enforces the vault). Quotes are
   read one month of trading days at a time and reduced by `required_quotes` to the rows the
   screener can read — the first quote at or after each intended fill, the last before each
   trading-day end and rollover, and the last one. Screening from the subset equals screening from
   all quotes (tested exactly), which keeps four years of ticks out of memory.
5. **Returns and metrics.** Daily net returns on every OOS trading day, zero before a strategy's
   first fill, so all strategies share one calendar. Conventions come from the gates: periods per
   year, 10,000 stationary-bootstrap resamples, Politis–White mean block length of at least 5
   days, one-sided p-values. Intervals (percentile, `ci_level`) for Sharpe, annual return, annual
   volatility, Sortino and maximum drawdown, all from the same resamples; trade statistics are not
   bootstrapped. The Sharpe ratio also gets its three standard errors, PSR, the minimum track
   record length at 95 % and the random-entry null p-value
   `(1 + #{null Sharpe >= Sharpe}) / (1 + seeds)` (ties count against the strategy, so a
   single-episode strategy such as buy-and-hold gets p = 1 by construction).
6. **Trials.** Every strategy is one trial of the hypothesis family on test folds, with its
   annualized Sharpe ratio and daily returns; the board's hash, dataset, strategy and parameters
   form its configuration. After all are recorded, the DSR of each uses the family's gated count
   (effective, per the gates); raw and effective counts are reported and a ratio above the review
   ratio is flagged. Re-running the board records its trials again: every evaluation counts.
7. **Report** under `reports/baselines/<dataset_id>/<run_id>/`: `board.md`, `board.json` (rows per
   strategy with their target, horizon and the base timeframe) and `returns.parquet` (daily net
   returns per strategy, for paired tests against later candidates), all run artifacts; key
   metrics are also logged as `board/<strategy>/<metric>`. Every net figure carries the cost
   model's label in the Markdown header, on every table row, in the JSON rows and in the CLI
   output.
8. **H-0001.** `experiments/hypotheses/H-0001.yaml` is a draft pre-registration of the board for
   the owner; it is registered only after review, before any run on real data.

## Consequences

- `xq baselines run --dataset <id>` regenerates the board in one command; the same seed gives the
  same numbers (tested), except the DSR, which gets stricter as the family's trials grow.
- On real data, the default board screens 24 strategies and 24,000 random-entry versions; the
  quote reduction keeps that tractable, and `random_entry_seeds` can be lowered for exploratory
  runs (never for a cited run).
- Undefined metrics (NaN) are reported as `n/a` and never logged to the registry, whose metric
  values must be finite.
