# ADR 0063 — Sprint 7, part 1: targets TGT-003 … TGT-006, WF-004 and WF-005

- **Status:** accepted (readings flagged for the owner's review: C-30; decided in ADR 0065,
  which replaces the trade/no-trade label's mirrored costs with the backtester's cost model)
- **Date:** 2026-10-04
- **Decided by:** Claude, within the plan (Phase 9, Phase 12, Sprint 7) and the owner's
  instruction for this session: Sprint 7 build-only, part 1 (TGT-003, TGT-004, TGT-005, TGT-006,
  WF-004, WF-005); every target passes the leakage harness; barrier labels recover known hit
  times on synthetic paths, with same-bar hits pessimistic and flagged; uniqueness weights match
  a hand example; synthetic data only
- **Tasks:** TGT-003, TGT-004, TGT-005, TGT-006, WF-004, WF-005

Synthetic data only. No target set has been materialized on real data, no walk-forward report has
been written for a real run, and nothing here is evidence. The FEAT tasks of Sprint 7 are part 2.

## Common ground: every new target is execution-aware and in sigma units

Every new kind uses the forward return's window (TGT-002, ADR 0025, ADR 0050): the entry fill is
the first market-hours quote at or after `t + latency` of market time, the exit fill the first at
or after `t + h + latency`; decisions while the market is closed get no label; a fill later than
`max_fill_delay_s` means no label (except a barrier touched before the intended exit, below).
`xq.targets.returns` now exposes that machinery (`ExecutionParams`, `label_windows`, `first_fill`,
`market_rows`, `horizon_scale`, `interim_sigma_rate`, `side_return`) instead of each kind
re-implementing it; forward returns are unchanged (code version 5, tests unchanged). Scales use
the interim sigma-hat known at t (TGT-002's EWMA, span 96 base bars) over the horizon,
`sigma_t * sqrt(h in market minutes)`, until a VOL-006 selection replaces it (C-18). Thresholds
are in sigma units or basis points, never dollars. Every target name starts with `tgt_`, which
the schema guard refuses in a feature matrix (TGT-001). Each kind is code version 1 and has its
own versioned target set in `config/targets.yaml`; a dataset still names one target set.

## TGT-003 — future realized volatility (`realized_vol`, `realized_vol.v1`)

- The mid is sampled on a grid of `sample_minutes` (5) of **market time** from the intended
  entry: the entry fill, then the last market-hours quote at or before each grid point (never
  before the entry), then the exit fill. The value is `sqrt(sum of squared log mid returns)`, the
  realized volatility over the window; `<name>_vol` divides it by sigma-hat over the horizon
  (realized over forecast).
- `label_end` is the exit fill, the last quote read. A horizon must be a whole number of
  sampling intervals; the kind is measured on the mid only.
- **Reading (C-30 (1)):** a window that crosses the daily break or a weekend includes the
  overnight return (the first point after the open). VOL-002's realized measures leave it out;
  TGT-003 keeps it because the target is the risk of holding over the horizon.

## TGT-004 — MFE and MAE in sigma units (`excursion`, `excursions.v1`)

The path is every market-hours quote after the entry fill up to and including the exit fill,
marked on the **exit side**: long `log(bid / ask_entry)`, short `log(bid_entry / ask)`.
`tgt_mfe_<ref>_<h>` is the best close-out, `tgt_mae_<ref>_<h>` the worst as a positive loss, both
divided by sigma-hat over the horizon. Both include the spread. A quote while the market is closed
is never on the path.

## TGT-005 — triple-barrier labels (`triple_barrier`, `barriers.v1`)

1. **Barriers.** Take-profit when the exit-side close-out return reaches `tp_sigmas * s`, stop
   when it falls to `-sl_sigmas * s` (s = sigma-hat over the horizon), vertical barrier at the
   forward return's exit fill. Side-specific; the spread is paid. Label +1 / -1 / 0.
2. **Three targets per window:** `tgt_tb_<ref>_<h>` (the label), `_t` (market minutes from the
   entry fill to the hit or the vertical barrier) and `_amb` (1 when the label came from an
   ambiguous bar). `label_end` is the hit quote, so purging removes exactly the data a label read.
3. **Same-bar hits.** `resolution: tick` (the repository's set) reads every quote, where one price
   cannot be on both sides of the entry: hits are exact and never ambiguous. A bar length
   (`1m`, `5min`, ...) reads the path as bars on the UTC grid, as with bar-only inputs: a bar
   touching both barriers resolves **pessimistically** to the stop and is flagged, and the hit is
   placed at the bar's last path quote (the event tier's bar mode does the same, ADR 0049).
4. **A hit needs no exit fill.** A barrier touched before the intended exit labels the window even
   when the exit fill is missing (the data ends, or the fill is late); only the vertical outcome
   needs it. The leakage harness caught the first version, which required the exit fill and so
   made an early label depend on quotes after its `label_end`. `LabelWindows` now carries each
   fill's timeliness; the label's `fill_delay_s` is the entry's when the exit fill is not used.
5. **Reading (C-30 (2)):** `tp_sigmas` and `sl_sigmas` are 1.0 and 1.0, provisional, fixed
   before any result. They are per target set; a strategy with another payoff gets another set
   version (C-22's break-even point depends on the ratio).

## TGT-006 — derived labels and uniqueness weights (`derived_label`, `derived.v1`)

1. **Derived labels** from the forward-return windows: `tgt_sign_<h>` (sign of the mid return),
   `tgt_big_<h>` (|mid return| above `big_move_sigmas` = 1.0 sigma-hats over the horizon) and
   `tgt_trade_<ref>_<h>` (1 when the side's execution-aware return beats the rest of the round
   trip's costs).
2. **Trade/no-trade costs.** A target kind cannot read the cost model, so the target set's params
   mirror `config/costs/placeholder.yaml` (PROVISIONAL) and a test fails if they drift:
   commission `2 * 0.035 USD/oz / entry mid`, slippage `2 * (0.5 bp + 0.1 * sigma-hat over one
   minute in bp)`, financing per close held over (`MarketClock.crosses_close`'s rule), at the
   side's rate / 360, three nights on Wednesday. **Reading (C-30 (3)):** the cost model's session
   multipliers (×3 in the rollover window, ×2 around US releases) are left out, so the label
   understates the slippage of trades entered or left in those windows; "expected net P&L" is read
   as the realized net return of the trade, with costs at their expected (placeholder) values.
3. **Concurrency and uniqueness** (`label_uniqueness`, `uniqueness_weights`). A label occupies
   `[label_start, label_end)`, in market time with a clock. Its concurrency is the time-averaged
   number of labels occupying its interval, its average uniqueness the time-average of
   1 / concurrency. Sample weights are the uniqueness scaled to a mean of 1 within the labels
   passed. Hand example (in the tests): A [0,4), B [2,6), C [5,7) have uniqueness 0.75, 0.625 and
   0.75 and mean concurrency 1.5, 1.75 and 1.5.
4. **Weights are computed in-fold, and known only at `weight_end`.** A label's uniqueness depends
   on every label overlapping it, so it is known at `weight_end`, the latest `label_end` among
   them (returned by `label_uniqueness`). Weights are therefore functions applied to a training
   set, not columns of `targets.parquet` (the builder computes targets a month at a time, and
   weights across a fold boundary would read the next fold's labels). **Reading (C-30 (4)):**
   ML-002 computes them on each fold's purged training labels; if weights are ever computed over
   labels reaching into a later window, purging must use `weight_end`.

## The leakage harness

Every configured target set joins `tests/leakage/test_targets.py` automatically (each spec's
value at t is unchanged when quotes after `label_end` are dropped, when quotes before t are
perturbed, and when sigma-hat at other times is shaken; sigma-hat is causal). Added: the
bar-resolved barrier (not in the repository's configuration) runs the same check, and the
uniqueness weights of forward-return and barrier labels are unchanged when quotes after
`weight_end` are dropped. Weights are not put through the "before t" checks: depending on labels
that started before t is information known at t, not look-ahead.

## WF-004 — the retraining schedule and stitching

1. `WalkForwardConfig.schedule`: `monthly` — the plan's research default, used when no `test_len`
   is given — or `fixed` (`test_len`, `step`). A monthly window runs from the start of the trading
   day dated the 1st (17:00 New York the evening before: 22:00 UTC in winter, 21:00 UTC in summer)
   to the next month's; the model is refitted at each window's start. Existing configurations
   name a `test_len` and are unchanged (the board keeps its 91-day fixed folds).
2. `stitch_oos` joins the folds' predictions: a decision time predicted twice, or a fold predicting
   outside its window, is refused; on a contiguous schedule (monthly, or a fixed step equal to the
   test length) so is a sample left unpredicted between the first and the last window. A fixed
   step longer than the window declares its gaps. `walk_forward` stitches through it.
3. WF-006's property test now draws monthly schedules as well and checks that contiguous windows
   abut and monthly windows start at a month's first trading-day start.

## WF-005 — the walk-forward report

1. `walk_forward_report`: per fold (training cutoff, test window, days, mean daily net return,
   Sharpe ratio, net return, share of positive days, the fold's recorded metrics); the fold Sharpe
   distribution over folds with a defined Sharpe ratio (folds without variance counted apart); and
   the decay regression of fold mean returns on time, `decay_trend` — the test behind R2's
   `decay_trend` gate — reported with its slope per year, t-statistic and one-sided p-value, from
   four folds.
2. A trading day belongs to the fold whose test window holds the day's start, or failing that its
   end (the first test day when the first window opens inside a trading day) — the same day a
   board assigns by its first out-of-sample decision.
3. `xq exp wf-report <run> --strategy <name>` (`board_report`): one strategy of a recorded baseline
   board run, from its fold-aligned `returns.parquet` and the fold windows its walk-forward
   evaluations recorded; a forecast-sign strategy carries its own evaluation's fold metrics.
   Written to `reports/walkforward/<run>/<strategy>.{md,json}`.
4. **Reading (C-30 (5)):** the report reads stored records of an existing run and records no run
   and no trial; its decay p-value is descriptive (no gate is judged; R2 is judged only by
   `xq validate-strategy`). Like the validation methods, it is not run on real results before a
   candidate exists and the owner allows it.

## Known truth (tests)

- `tests/unit/targets/test_volatility.py`: equal to the realized volatility computed by a plain
  loop on synthetic paths, with a quote gap and across the daily close (the gap included).
- `tests/unit/targets/test_excursion.py`: hand-computed MFE and MAE on both sides, a steady rise,
  a stray quote in the daily break left off the path.
- `tests/unit/targets/test_barrier.py`: known hit times (take-profit and stop at the 41st quote,
  20 minutes in), the vertical barrier at the exit fill, the short side, random paths against a
  brute-force search, a bar touching both barriers in either order resolved to a flagged stop
  while ticks resolve it exactly, a weekend gap through the stop.
- `tests/unit/targets/test_weights.py`: the hand example, identical and disjoint labels, market
  time over the daily break, hand-computed derived labels including a triple-Wednesday night, the
  costs against the placeholder model.
- `tests/integration/targets/test_sprint7_targets.py`: a dataset with each new target set.
- `tests/unit/validation/test_retraining.py`, `tests/property/test_splitters.py`: monthly windows
  in both seasons; the stitched series has no overlaps or gaps; the refusals.
- `tests/unit/validation/test_walkforward_report.py`,
  `tests/integration/validation/test_walkforward_report.py`: per-fold figures, a planted decay
  found and a stable edge not flagged, and the report of a synthetic board run through the CLI.

## Consequences

- Target sets v1 of four new kinds exist; none is materialized on real data, and no dataset spec
  in `experiments/configs/` names one yet (ds_base keeps `fwd_returns.v1`).
- The weights wait for ML-002 to be applied in-fold; meta-labels stay ML-008.
- Speed on four years of real ticks is unmeasured: the barrier kind loops over decisions in Python,
  and the realized-volatility grid holds up to 276 points per decision for a 1d horizon.
