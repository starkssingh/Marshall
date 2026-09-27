# ADR 0027 — Splitters, purging and embargo

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** WF-001 (guarded by WF-006; used by WF-002, VAL-003)

## Context

WF-001: `WalkForwardSplitter(mode, min_train, val_len, test_len, step, purge_by="label_end",
embargo)` yielding folds with train, validation and test indices, `train_end`, `test_start` and
`test_end`; `PurgedKFold` for inner CV; `CombinatorialPurgedCV(n_groups, k_test)` for PBO. Failure
condition: any fold where training labels end inside the test window.

## Decision

1. **Samples** are the dataset's decision times (unique, increasing, tz-aware) with the target's
   `label_end`. A sample without a label is never trained or validated on, but is still predicted
   in its test window.
2. **Walk-forward windows are calendar spans** (`"365D"`-style durations). Test windows
   `[test_start, test_start + test_len)` start `min_train + val_len` after the first sample and
   advance by `step` (default `test_len`). `step < test_len` is refused, so every out-of-sample
   prediction belongs to exactly one fold. Windows without test samples, or without training
   samples after purging, yield no fold.
3. **Validation precedes test.** The validation window is the `val_len` right before the test
   window; training is everything before it (`expanding`) or the `min_train` before it
   (`rolling`).
4. **Purging by `label_end` with the embargo as a gap.** Training labels must end before
   `validation start − embargo`, and validation labels before `test start − embargo`. So the latest
   label used for fitting or selection always ends more than `embargo` before the test window.
   The splitter asserts this for every fold, and WF-006 property-tests it.
5. **`train_end`** is the latest `label_end` among a fold's training and validation samples: the
   information cutoff. The prediction store (WF-003) refuses predictions whose decision time is
   not after `train_end + embargo`.
6. **Purged k-fold and CPCV** use contiguous groups. A training sample is dropped when its label
   interval `[t, label_end]` meets a test group's span `[first test time, last test label_end +
   embargo]`. This purges overlapping labels before the group and embargoes the samples after
   it. CPCV holds out every combination of `k_test` groups, purged against each group
   separately. Backtest-path reconstruction for PBO comes with VAL-003.
7. **No shuffling anywhere**: none of the splitters accepts a random state.

## Consequences

- Durations as calendar spans make windows easy to read ("train two years, test one month"). A
  window over a holiday period simply holds fewer samples.
- The embargo is a pure safety margin on top of purging (for serially correlated features). It
  costs `embargo` of training data per fold and never touches test samples.
