# ADR 0058 — The baseline-board subject adapter for `xq validate-strategy`

- **Status:** accepted
- **Date:** 2026-09-28
- **Decided by:** Claude, within the plan (Phase 17's acceptance: "`xq validate-strategy
  <run_id>` produces significance and robustness reports for any baseline or candidate") and the
  owner's C-25 instruction to build it next
- **Tasks:** `xq validate-strategy` (Phase 17), ROB-008; closes C-25 (7)

Synthetic data only. Nothing has run on real data, and no board has run on real data.

## Context

`xq validate-strategy` rebuilds the strategy a run chose as a `StrategySubject` through a subject
adapter per run kind (ADR 0056). Only simulated runs had one. The baseline board is the first real
research run kind: it evaluates every baseline through the same folds, costs and metrics
(BASE-005), and the gates' R1 test compares every later candidate with it. Its adapter needs the
board's screening context (quotes, cost model, clock, sigma-hat and rule parameters) rebuilt from
the run.

## Decision

1. **One screening context, shared.** `xq.models.board.screening_context` builds what the board
   screens against: the walk-forward folds and their out-of-sample decisions and days, the usable
   quotes, the cost model, the market clock and the sigma-hat of 1-minute returns.
   `run_baseline_board` now uses it, and the adapter (`xq.validation.subjects.board_subject`)
   rebuilds it from the run's recorded board, targets and dataset. The rule baselines' positions
   come from the same `rule_signal_bars` and `rule_positions`. There is one code path, not a
   copy.
   - A dataset whose directory is gone is rebuilt from its recorded spec and must build the same
     id, as `xq exp reproduce` requires.
   - The context also keeps the quotes a screen with ROB-002's added latencies (250 ms, 1 s,
     5 s) would read. The screener's lookups land on the same quotes either way, so the nominal
     screen is unchanged; the check in item 2 confirms it on every validation.
2. **Rebuilt, then checked against the record.** One strategy of the board is validated at a
   time (`--strategy`; without it the run's strategies are listed).
   - A rule (`rule` or `rule_vol`) is recomputed from the dataset's signal bars.
   - A forecast-sign strategy is rebuilt from the predictions the run stored.
   - Its daily net returns on every out-of-sample day must equal the ones the run recorded in
     `returns.parquet`, within `1e-9 + 1e-6 |x|`. If they differ, the code or the data changed
     since the run, and the validation is refused: reproduce the run first.
   - The artifacts read (`returns.parquet`, the predictions) are checked against the SHA-256 the
     registry recorded. An altered or missing file is refused.
3. **The family and the baselines.** The family is every strategy of the board with its recorded
   daily returns; PBO, SPA and the Holm table read it, and the DSR reads the registry's trials of
   the hypothesis's family. R1's baselines are the board's **other** strategies: a baseline judged
   as a candidate must beat the best of the rest.
4. **Parameters, per C-25 (4).** Board strategies have no tuned parameter.
   - A rule's numeric constants (its `params`, and the volatility target of a `_vol` rule) are
     its neighbourhood, unless the hypothesis declares them fixed a priori with a source.
   - A perturbed point the rule refuses (a fast average no longer faster than the slow one)
     never trades: it counts as not profitable, the conservative direction.
   - A forecast-sign strategy's model is not refitted at perturbed constants. Its neighbourhood is
     **not evaluated**, with that reason, so its R2 verdict is incomplete unless its parameters
     are declared fixed a priori. Refitting belongs with the model runs (ML-009, MREG-001).
5. **Re-evaluation.**
   - Execution delay shifts the target positions by whole decision bars (ROB-007).
   - Cost stress screens the positions under ROB-002's grid and the R2 scenario.
   - Price noise disturbs a rule's signal bars by multiples of the median quoted spread, while
     the fills stay on the true quotes (ROB-005). Feature noise does not apply to rules (they
     read only prices), and no noise applies to forecast-sign strategies (their models would need
     refitting).
6. **Trades and sigma-hat.**
   - The trades are the screen's closed holding episodes. `trade_return` is the episode's net
     P&L over its notional at entry.
   - `sigma_daily` is the sigma-hat of 1-minute returns at the entry decision, scaled by the
     square root of the minutes in a regular trading day (23 hours). One R in the Monte Carlo is
     `stop_sigmas` of it.
   - Episodes entered before any sigma-hat is known are left out of the trade list; they stay in
     the returns.
   - The daily sigma-hat of the volatility-tercile slices is the one known at each day's first
     out-of-sample decision.
7. **Labels.** Every net figure carries the cost model's label ("screening, placeholder costs"
   while costs are placeholders). The adapter cannot tell synthetic ticks from real ones, so the
   report has no synthetic banner. The runs in the tests are exploratory.

A latent bug surfaced on the first real screen: ROB-001's median-to-nominal ratio is NaN when the
nominal Sharpe ratio is not positive (the simulated candidates are always positive), and the
report's JSON refused it. It is now stored as null, like the report's other undefined values.

## Consequences

- `VALIDATABLE_KINDS` is `simulated_strategy` and `baseline_board`. Other kinds are still refused
  by name.
- A board strategy validated on the fixture's three weeks of synthetic ticks fails most gates,
  and PBO is not evaluated (fewer than 32 days). That is expected on such a short sample: the test
  checks the machinery, never the verdict.
- A validation re-screens the strategy several hundred times (the neighbourhood grid and its heat
  maps, cost stress, delays and noise). The speed on four years of real ticks is unmeasured.

## Known truth (`tests/integration/validation/test_validate_board.py`)

On a board of three rules with their volatility-targeted versions and two forecast baselines,
over three weeks of synthetic ticks:

- without `--strategy`, and with an unknown one, the validation is refused;
- `ma_4_12_vol` rebuilds exactly (the record check passes). Its neighbourhood is its five
  constants (fast, slow and the three volatility-target values) and is gated. The report carries
  the screening label, R1 reads the paired test against the best other strategy, and no trial is
  added;
- `historical_mean:fwd_ret_mid_1h` has its neighbourhood not evaluated with the adapter's reason,
  and its R2 verdict is not `pass`;
- under a hypothesis declaring its parameters fixed a priori, `tsmom_8`'s neighbourhood is not
  applicable with the source named, its perturbation reported but not gated, and every other
  robustness gate still judged;
- an altered `returns.parquet` is refused.
