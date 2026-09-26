# ADR 0019 — Quality gate in the dataset builder

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DQ-007 (with DS-005, ADR 0017; thresholds per ADR 0011 and ADR 0013)

## Context

DQ-007: the dataset builder refuses FAIL partitions unless explicitly excluded (exclusion
recorded); WARN partitions are listed in the dataset manifest. Acceptance: the gate blocks a
partition with an injected OHLC error.

## Decision

1. **Every partition a dataset touches is gated.** The partitions are the trading days of every
   bar the builder reads: warm-up, window and context timeframes (a 4h or daily context bar is
   built from its trading day's ticks). Days the spec excludes are gated too, so the manifest
   records their failing checks.
2. **One quality run.** The gate reads the results of the run pinned by the resolved spec (ADR
   0017), never "whatever is latest" at read time, so the gate decision is reproducible from the
   dataset id.
3. **Refuse, explain, and let the owner decide.** A FAIL day that is not excluded, or a day with no
   results in the run, refuses the build; the error names every such day with its failing checks
   and says how to proceed (exclude it in the spec with a reason, or re-run `xq validate`). The
   gate never excludes anything by itself: dropping data silently could bias a dataset.
4. **Exclusion removes the partition everywhere.** Bars of an excluded day are dropped from the
   base and every context timeframe before features are computed; later rows then see the
   previous available context bar, exactly as if the data were missing.
5. **Manifest.** `included_partitions` (count), `warn_partitions` (day and warning checks) and
   `excluded_partitions` (day, reason, failing checks) sit next to `quality_run_ids`.
6. **Partition vs decision day.** Exclusions and the gate work on the bar's trading day. The
   `trading_day` column in the features is the trading day of the decision time (ADR 0018): the
   last bar of a day closes at 17:00 New York, which already belongs to the next trading day. So
   an excluded day can still appear as the calendar `trading_day` of one row: the close of the
   previous day's last bar.

## Consequences

- A dataset can only be built from validated data, and every compromise (excluded or WARN days) is
  visible in its manifest.
- Until the DQ-008 real-data review, the gate enforces the ratified provisional thresholds (ADR
  0013); a later threshold change gives a new quality run and so new dataset ids.
