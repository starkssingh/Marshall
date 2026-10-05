# ADR 0064 — Rule warm-ups on signal bars before the dataset's start (C-29)

- **Status:** accepted
- **Date:** 2026-10-05
- **Decided by:** project owner (C-29, approved as worded below); implemented by Claude. Amends
  ADR 0061, decision 3, and the reading in ADR 0062, C-26 (6)
- **Tasks:** BASE-005 (the board runner); `xq validate-strategy`'s board adapter (ADR 0059)

Synthetic data only. No board has run on real data, and H-0001 is still unregistered.

## Context

Under ADR 0061 a rule's warm-up counted on the dataset's own signal bars, and each rule's
evaluation period began at the first decision after its own warm-up bar. With `ds_base` starting
on 2015-01-01 and data downloaded from 2014-01-01, the 2014 bars served the dataset's 10-day
`warmup`, sigma-hat and the quality report, but not the rules: `tsmom_252@1d` would have been
first evaluated about a year after the start, `ma_crossover_50_200@1d` about 200 trading days
after it, and every rule over a different period (ADR 0062, C-26 (6)).

## Decision (owner)

1. **Rule warm-ups may read signal bars before the dataset's start**, from the same source, the
   same bar build and price basis, before the vault, and quality-gated like any other bars, **up
   to each rule's warm-up length** (`rule_warmup`, from its parameters, unchanged).
2. **Every rule's evaluation period starts at the dataset's first trading day** (for `ds_base`,
   2015-01-02, the first decision of the dataset), so all rules share one evaluation start.
3. **The board stops with an error** (`BoardError`) if the pre-start bars a warm-up needs are
   missing or fail the quality gate.

## Implementation

- `pre_start_bars(cfg, engine, spec, timeframe, before, count, name)` reads, through the catalog
  (so the vault is enforced), the dataset's source, instrument, price basis and pinned bar build:
  complete bars only, without the trading days the spec excludes, available before the dataset's
  first signal bar, and keeps the last `count`. It loads from the start of the open trading day
  that the warm-up needs (`count` divided by the bars in a regular trading day, plus
  `PRE_START_MARGIN_DAYS` = 5 open days for early closes and gaps), never more than it keeps.
  - Every trading day those bars touch goes through the quality gate (`gate_partitions`, DQ-007)
    with the dataset's pinned quality run: a FAIL day or a day the run never graded stops the
    board, naming the rule and the days.
  - Fewer than `count` complete bars (the data starts later, or gaps) stop the board, naming the
    rule, the timeframe and how many bars it needs and has.
- `warmed_signal_bars(cfg, engine, context, name, timeframe, warmup)` prepends exactly as many
  pre-start bars as rule `name` needs for its warm-up bar to be available at the dataset's first
  decision: `warmup` minus the dataset's own signal bars available by then (one, the context bar
  of the first decision). A rule never reads more pre-start history than its warm-up length, so
  a path-dependent rule (z-score hysteresis, Donchian stops) starts from the state its warm-up
  defines, not from an arbitrary longer history.
- Every rule's `evaluation_start` is the dataset's first decision and its evaluation days are
  every trading day of the dataset. The report shows `Warm-up bars (pre-start)` (for example
  `253 (252)`), and `board.json` carries `pre_start_bars` per rule.
- **The warm-up guard** now reads the rule's exposure on its signal bars: a non-zero exposure
  before the warm-up bar means the formula is wrong and stops the board (the guard on decisions
  before the evaluation start is vacuous now that every rule starts at the first decision).
- **`xq validate-strategy`** rebuilds a rule from the same warmed signal bars, so its fold-aligned
  returns still equal the recorded ones. A neighbourhood point with a longer warm-up reads the same
  bars and stays flat a little longer; on real data its test days are years after the start.
- **Unchanged:** the vault evaluation (its warm-up history is the dataset's own, years before the
  vault), bundles, the fold-aligned view, forecast-sign strategies (still on the test folds), the
  random-entry template (the positions from the evaluation start, now the first decision).

## Known truth (tests)

- `tests/integration/models/test_baseline_board.py` (ticks begin a week before the dataset):
  every rule on 1h and 4h bars starts at the dataset's first decision with `warmup - 1` pre-start
  bars; the warmed bars are the catalog's latest complete bars of the same build before the
  dataset's first signal bar, the warm-up bar available at the first decision; a 400-bar
  momentum rule stops the board for missing pre-start bars; failing the quality results of the
  pre-start days stops it for gate-failed bars; a warm-up formula two bars too long is caught by
  the exposure guard.
- The validation, reproduction, walk-forward report and registry suites start their ticks a week
  earlier for the same reason and pass unchanged otherwise.

## Consequences

- On `ds_base`, `tsmom_252@1d` warms up on 252 daily bars of 2014 and every rule is evaluated from
  2015-01-02. The owner's 2014 download must cover each rule's warm-up and pass `xq validate`
  (C-8); a missing or failing 2014 day under a warm-up stops the board.
- Board runs recorded before this change evaluated rules from their own warm-up bar; their
  returns differ (only synthetic test runs exist).
