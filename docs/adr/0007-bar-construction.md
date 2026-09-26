# ADR 0007 — Bar construction: alignment, exclusions, storage and versioning

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DATA-008 (the plan defers the 4h/1d alignment decision to an ADR)

## Context

DATA-008 builds bid, ask and mid OHLC bars on 1m, 5m, 15m, 30m, 1h, 4h and 1d from clean ticks,
with `available_at`, a gaps table, `is_complete`, and bit-identical rebuilds. The plan asks for
5m–1h to be aggregated from 1m and for 4h/1d to align to the 17:00 New York trading day.

## Decision

1. **Intervals and availability.** A bar covers `[bar_start, bar_end)`, is labelled by its start
   and carries `available_at = bar_end + bars.publication_latency_ms` (default 0 ms). All instants
   are UTC int64 nanoseconds.
2. **Alignment.** 1m–1h bars align to UTC. 4h and 1d bars align to the trading-day start (17:00
   New York), so 4h bars start at 17:00, 21:00, 01:00, 05:00, 09:00 and 13:00 New York. An
   anchored bar never extends past its trading day's end. Because 17:00 New York falls on a whole
   UTC hour, bars of 1h or less never straddle a trading-day boundary either. `trading_day` is the
   trading day of the bar start.
3. **Every timeframe is built from ticks** rather than 5m–1h from 1m. A median cannot be
   aggregated exactly from finer medians, and building from ticks keeps every statistic exact with
   a single code path. A property test shows that aggregating 1m bars (1h for 4h/1d) reproduces
   every additive field of each higher timeframe: OHLC for all three bases, `tick_count`,
   `n_flagged`, maximum and closing spread, and the tick-weighted mean spread.
4. **Mid** is computed per tick as `(bid + ask) / 2`, then aggregated, so `mid_high` is the highest
   mid actually quoted, not the average of `bid_high` and `ask_high`.
5. **Exclusions.** Ticks whose flags intersect `bars.exclude_flags` (default `MISSING_QUOTE`,
   `NONPOSITIVE`, `CROSSED`, `DUP_EXACT`) do not enter prices and are counted in `n_excluded`.
   Only causal flags may be listed. `SPIKE` is refused, because its confirmation needs later ticks
   and excluding it would give research bars information a live system cannot have (ADR 0006).
   Flagged ticks that are included are counted in `n_flagged`. `n_excluded` counts only excluded
   ticks inside bars that exist, so it is not additive across timeframes.
6. **No filled bars.** An interval without included ticks produces no bar. Consecutive-bar gaps
   are written to `bar_gaps`, and `expected_open` is true if any part of the gap lies inside the
   calendar's market hours.
7. **Completeness.** `is_complete` is true when the bar's end is at or before the latest instant
   the source's ingested data reaches, i.e. only bars at the data frontier are in progress. A bar
   thin because data is missing in the middle of history is complete; its gap and `tick_count` tell
   the story.
8. **Storage.** One Parquet file per timeframe, build version and month of the trading day:
   `data/bars/<source>/<instrument>/tf=<tf>/build=<version>/year=YYYY/month=MM/part.parquet`. Each
   row carries all three bases (`bid_*`, `ask_*`, `mid_*`), so a `bar_sets` row describes one
   timeframe with `basis = "bid+ask+mid"`; `load_bars(..., basis=...)` (DATA-010) selects one.
   Columns beyond the plan's schema: `n_excluded`.
9. **Versioning.** `build_version = "<bars.version>-<8 hex>"`, the hash covering the bar settings,
   `BAR_CODE_VERSION` and the clean rules version, so bars from different cleaning rules never
   overwrite each other. `bar_sets.sha256` hashes the set's file hashes in path order.

## Consequences

- The plan's "5m from 1m equals 5m from ticks" check is kept as a property test, with the median
  and `n_excluded` explicitly outside it.
- A partial rebuild (`--start/--end`) can leave `is_complete` stale in months it did not touch;
  the default full rebuild does not.
