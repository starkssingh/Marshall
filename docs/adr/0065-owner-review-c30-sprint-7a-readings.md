# ADR 0065 — Owner review of Sprint 7 part 1's readings (C-30)

- **Status:** accepted
- **Date:** 2026-10-05
- **Decided by:** project owner, reviewing the five readings of ADR 0063 (C-30); (3) and (4)
  implemented by Claude. Amends ADR 0063, items TGT-003, TGT-006 (2) and (4)
- **Tasks:** TGT-003, TGT-005, TGT-006, WF-001, WF-005

Synthetic data only. No target set has been materialized on real data.

## (1) TGT-003 includes gap returns; VOL-002 excludes them — approved

TGT-003's future realized volatility includes the return across the daily break or a weekend when
its window crosses one (the first point after the open), because the target is the risk of holding
over the horizon. VOL-002's realized measures (RV from 1m and 5m returns per hour and trading
day) leave those returns out; only Yang-Zhang carries them.

**The two are different quantities.** Every volatility evaluation names its target explicitly:
a forecaster scored on `realized_vol.v1` (gaps included) is not compared with one scored on
VOL-002's RV (gaps excluded), and a report states which one it uses. A sigma-hat fitted to
VOL-002's RV understates the risk TGT-003 measures over a weekend (the known issue in
`docs/STATUS.md`).

## (2) Barrier multiples per target set version — approved

`barriers.v1`'s take-profit and stop of 1.0 and 1.0 sigma-hats over the horizon stay as fixed
before any result. A strategy with another payoff gets **a new pre-registered target set
version** (`barriers.v2`, ...), never a change to `v1`'s multiples (the target-set lock refuses
that anyway).

## (3) The trade/no-trade label calls the backtester's CostModel — FIX

The owner rejected the mirror: the label understated slippage in the rollover and US-release
windows. Now:

1. **The label is priced by the backtester's own `CostModel`.** `derived_label`'s parameters
   name a cost model (`cost_model: placeholder`) instead of copying its numbers;
   `xq.targets.kinds.target_specs(cfg, definition, instrument)` binds
   `CostModel.from_config(cfg, instrument, name=...)` to every spec (`TargetSpec.costs`, a new
   optional field; `TargetKind.cost_model` says which kinds need it). The dataset builder and
   the leakage harness build specs there. A trade spec without a bound model refuses to compute.
2. **The same arithmetic as the screener** (`round_trip_pnl`): a one-lot round trip on the side,
   entered at the window's entry fill and left at its exit fill:
   - fill prices are the ask and the bid moved by `CostModel.slippage_bps` at each fill time —
     the session and event multipliers included (×3 in the rollover window, ×2 around US
     releases);
   - commission is `CostModel.commission_usd` of each fill at its mid;
   - financing is `CostModel.financing_usd` at every rollover `CostModel.rollovers` lists after
     the entry fill up to and including the exit fill (the screener's "held at a rollover"),
     three times on the triple weekday, marked at the mid of the last quote before it.

   The label is 1 when the net P&L is positive. "Expected net P&L" is read as the trade's
   realized net P&L with the model's costs (the reading of ADR 0063, unchanged).
3. **One remaining difference, documented:** slippage's sigma term reads the sigma-hat known at
   the decision t for both fills; the screener reads it at each fill's own decision. The label is
   defined at t.
4. **Fills agree.** `target_specs` refuses a cost model whose latency or maximum fill delay
   differs from the target set's execution parameters, so the label's fills are the
   backtester's.
5. **Versions.** The kind's code version goes from 1 to 2. The target set's definition changed,
   so it is a new version: `derived.v2` replaces `derived.v1`, which was never materialized
   outside synthetic tests and is removed from `config/targets.yaml`. The dataset's config digest
   now covers the named cost model's configuration, so a change to `config/costs/<name>.yaml`
   changes the id of every dataset whose labels it prices.

**Known truth:** a long entered at 16:50 New York and left 15 market minutes later at 18:05 (both
fills inside the 16:45–18:15 rollover window, held over the 17:00 rollover) pays three times the
slippage in the label and in `run_vectorized`, and the label's per-lot net P&L equals the
screener's trade P&L divided by its lots; the hand-computed round trip and the triple-Wednesday
financing are reproduced; the bound model is the configured one, and an unbound spec, a
mismatched fill delay and an unknown model are refused (`tests/unit/targets/test_weights.py`).
Every derived label still passes the leakage harness, and `derived.v2` builds a dataset.

## (4) Purging uses max(label_end, weight_end) — approved, with a test

Uniqueness weights stay in-fold functions (ML-002), not columns of `targets.parquet`. When weights
are computed over labels that reach into a later window, a sample is purged by
`max(label_end, weight_end)`: `WalkForwardSplitter.split`, `PurgedKFold.split` and
`CombinatorialPurgedCV.split` take an optional `weight_end` and use that maximum for every purge,
embargo and the `train_end` cutoff. **Known truth** (`tests/unit/validation/test_splitters.py`):
on overlapping two-hour labels, purging by `label_end` alone keeps a training label whose weight
reads a label ending at the embargoed test start; purging by the maximum drops exactly that one,
and every remaining training weight is known before the cutoff, in walk-forward and in purged
k-fold.

## (5) `xq exp wf-report` — approved

It reads a recorded run's stored returns and folds, records no run and no trial, and its decay
p-value is descriptive; R2 is judged only by `xq validate-strategy`. Unchanged.

## Consequences

- `config/targets.yaml` holds `derived.v2`; no dataset spec names it (ds_base keeps
  `fwd_returns.v1`).
- The cost model's numbers no longer appear in a target set: when broker terms replace the
  placeholder model, the labels follow it under a new dataset id.
