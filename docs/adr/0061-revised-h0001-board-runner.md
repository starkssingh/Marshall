# ADR 0061 — The revised H-0001 in the board runner (C-15)

- **Status:** accepted (two readings flagged for the owner's review: C-28); decision 3's
  per-rule evaluation start is amended by ADR 0064 (C-29: pre-start warm-ups, one start)
- **Date:** 2026-10-03
- **Decided by:** Claude, implementing the owner's H-0001 revision (ADR 0035) with the readings
  the owner approved (ADR 0041), on the owner's instruction for this session
- **Tasks:** BASE-005 (C-15); `xq validate-strategy` (ADR 0059); MREG-003, MREG-004, GATE-002
  (ADR 0060)

Synthetic data only. H-0001 is **not registered**: its discovery and evaluation windows are still
"set from the real data's depth at registration", which is the owner's (C-15). No board has run on
real data.

## Context

ADR 0035 revised the H-0001 draft: rule baselines evaluated over the full pre-vault history after
each rule's own warm-up, with the fold-aligned version stored for comparison; rules on 1d and 1h
signal bars; a trial budget of 36; descriptive slices by year and by session. ADR 0041 approved the
two readings (lookbacks count bars of the signal timeframe; the fold-aligned version is not a
trial). The board runner still evaluated every strategy on the walk-forward test folds only, on
one signal timeframe.

## Decisions

1. **Signal timeframes and names.** The board configuration's `signal_timeframe` is replaced by
   `signal_timeframes` (`[1d, 1h]` in `experiments/configs/baselines/board.yaml`). The old key is
   refused, not read as a legacy alias; a board with rules needs at least one timeframe, without
   repeats. Every rule strategy is named `<name>@<timeframe>` (`tsmom_252@1d`,
   `tsmom_252_vol@1h`) on every board, and `BoardConfig.strategies()` returns a `BoardRule` (the
   rule's configuration and its timeframe). 6 rules × plain and volatility-targeted × 2
   timeframes = 24 rule trials, plus 4 targets × 3 forecast-sign strategies = 36, H-0001's trial
   budget; a test pins the count on the repository's board.
2. **Warm-up, from the parameters** (`xq.models.baselines.rule_warmup`), never from observed
   exposure: the signal bars a rule needs before its state is defined — `buy_and_hold` 1,
   `time_series_momentum` `lookback + 1`, `zscore_reversion` `lookback`, `ma_crossover` `slow`,
   `donchian_breakout` `max(entry, exit, atr_window) + 1`, and `lookback + 1` of the volatility
   target for a `_vol` rule, whichever is larger. A unit test per rule shows, on steadily trending
   bars, that the first non-zero exposure lands exactly on the warm-up bar.
   - **Donchian waits for every channel.** It used to enter as soon as its entry channel and ATR
     were known, before a longer exit channel was. It now enters only once the exit channel is
     known too, so its state is fully defined from the warm-up bar. Only configurations with
     `exit > max(entry, atr_window)` change; no board has one (the repository's is 20/10/20), and
     the unit test that exercised entry with an unknown exit channel now uses a known one.
   - **Runtime guard.** A rule holding a position at a decision before its evaluation starts
     stops the board with a `BoardError` (the formula would be wrong). A dataset with fewer signal
     bars than a rule's warm-up, or no decision after its warm-up bar, stops it too, naming the
     rule.
3. **Evaluation period.** Rules are screened over **every** decision of the dataset: the
   screening context now holds every decision and its trading days, and its quotes and sigma-hat
   cover the full span. A rule's evaluation period (`full_history`) is the trading days from the
   first decision at or after its warm-up bar's `available_at` to the dataset's end (before the
   vault). The board's statistics, the DSR, the trial's returns and the random-entry null use that
   period. The random-entry template is the positions on the evaluation decisions only, so random
   episodes never land in the warm-up. Forecast-sign strategies stay on the walk-forward test
   folds (`test_folds`): their models are fitted.
4. **Fold-aligned view.** The same full-history screen's daily net returns restricted to the
   out-of-sample (test-fold) days: not a separate screen and not a trial (ADR 0041).
   - `returns.parquet` (kind `baseline_returns`) stays the fold-aligned matrix: every strategy on
     every OOS day, no missing value, so its consumers keep identical days.
   - `returns_evaluation.parquet` (kind `baseline_returns_evaluation`) holds each strategy's
     evaluation-period returns on every trading day of the dataset, missing outside its period.
   - Report columns: signal timeframe, warm-up bars, evaluation start and days, period
     (`full_history` or `test_folds`), and the fold-aligned Sharpe ratio, annual return and net
     P&L as a descriptive comparison, without p-values.
   - Consequence: in the fold-aligned view a rule enters the test days already holding the
     position its history implies, where the old OOS-only screen opened it at the first OOS
     decision. Fold-aligned returns therefore differ from what the old runner gave for the same
     rule (no board has run on real data).
5. **Slices.** `xq.robustness.slicing` (`run_slices`, `slice_pnl`) breaks down each strategy's
   evaluation-period daily net P&L and the closed trades entered in its period by the slices the
   run's hypothesis declared in its registered text (H-0001: year, session). The year and session
   tables are in `board.json` and `board.md`, labelled descriptive: no p-values, no trials. A
   slice that cannot be computed (a `SliceError`, such as a regime slice before REG-007) is
   reported per strategy, not fatal. A declared volatility slice reads the daily sigma-hat at each
   day's first evaluation decision.
6. **Downstream.**
   - **`xq validate-strategy`** (ADR 0059): the screening context carries every decision and
     day. A rule is rebuilt from its own timeframe's signal bars, screened over every decision,
     and restricted to the OOS days, which must equal the recorded fold-aligned returns. Every
     re-evaluation (neighbourhood, delay, noise) screens the full history the same way and reads
     the OOS days. `cost_stress` takes optional `days`, so its Sharpe ratio, P&L, costs and
     break-even multiplier read the OOS days only. The trades are the closed episodes entered at
     or after the first OOS decision. The daily sigma-hat for volatility terciles is read on the
     OOS decisions (the context's sigma-hat now spans every decision). The price-noise level's
     median quoted spread is now taken over the full span's screened quotes.
   - **Bundles:** a bundle's `signal_timeframe` is its rule's, not the board's.
   - **Vault:** the warm-up load length and the signal bars use the bundle's `signal_timeframe`.
   - **Reproduce:** unchanged; a board run still reproduces on the same code.
   - Board runs recorded before this change carry the old `signal_timeframe` key and are refused
     by the new configuration model (only synthetic test runs exist).

## Readings for the owner's review (C-28)

1. **Where each record is judged.** The H-0001 board test (its Sharpe p-value, DSR, random-entry
   null and slices) uses each rule's full history after its warm-up. `xq validate-strategy`
   (R1 and R2), the registry's `backtest` history (MREG-004) and the vault's walk-forward interval
   (R3) keep judging a rule on its fold-aligned record: identical days for every strategy and for
   every later candidate, and the conservative choice (fewer days, no record a fitted candidate
   could not have). Claude's reading, for review: should validation and the registry move to the
   full history for rules instead?
2. **The first evaluation day.** The evaluation starts at a decision, but daily returns are by
   trading day, so the first day counts only from that decision (the rule is flat before it, by
   the guard). Claude's reading: the day is kept, as a day whose earlier decisions were flat.

## Known truth (tests)

- `tests/unit/models/test_rule_baselines.py`: the warm-up of each rule (and its `_vol` version)
  is the bar of its first position on a trend; Donchian waits for every channel.
- `tests/unit/models/test_board.py`: names `<name>@<timeframe>` on every timeframe; the old key,
  a board with rules and no timeframe, repeats, unknown timeframes and `@` in a rule's name are
  refused; the report renders the new columns, the fold-aligned table and the slices.
- `tests/integration/models/test_baseline_board.py` (1h and 4h signal bars on three weeks of
  synthetic ticks): every rule's evaluation starts at the first decision after its warm-up bar,
  before the test folds; the fold-aligned matrix equals the evaluation returns on the OOS days;
  trials equal strategies; year and session slices are present and add up; a regime slice is
  reported, not fatal; a too-short dataset and a wrong warm-up formula stop the board; the
  repository's board gives exactly 36 strategies.
- `tests/integration/validation/test_validate_board.py`: a 1h and a 4h rule rebuild exactly; a
  rule's trades start on the test days, and its cost stress equals its fold-aligned record.
- `tests/unit/robustness/test_costs_stress.py`: with `days`, every figure reads those days.
- `tests/integration/registry/test_vault.py`: a 4h rule bundle carries `signal_timeframe` 4h and
  goes through the vault procedure; the gate and reproduce suites pass with the new names. The
  leakage harness is untouched.
