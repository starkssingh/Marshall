# ADR 0009 — Hour-of-week spread statistics

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DATA-009

## Context

DATA-009 stores spread percentiles (p50, p90, p99) per hour of week and source for the cost-model
fallback (BT-001) and the DQ spread-outlier check. The plan does not say in which time zone the
hour of week is counted, which ticks count, how percentiles are computed, or whether vault data
may be used.

## Decision

1. **New York hour of week.** `hour_of_week = weekday * 24 + hour` in America/New_York local time,
   Monday 00:00 = 0. The 17:00 rollover, and the widening around it, always land in the same
   buckets whatever the DST state; a UTC clock would move them by an hour twice a year.
2. **Which ticks.** Clean ticks of the configured rules version, minus those carrying any
   `bars.exclude_flags` flag, so statistics describe the same quotes bars are built from.
   Flagged-but-usable ticks (e.g. `SPREAD_OUTLIER`) are included: a spread that really was quoted
   is a real cost.
3. **Exact percentiles.** Prices sit on the tick grid, so spreads are counted as integer multiples
   of `tick_size` in a histogram per hour. Percentiles use the inverted-CDF definition (smallest
   spread with at least q of the ticks at or below it) and are exact, with memory bounded by the
   number of distinct spreads rather than the number of ticks.
4. **Vault excluded.** Only ticks before `vault.start` are used. These statistics are research
   inputs; computing them over the vault would let the holdout shape the cost model.
5. **Windows.** Rows are keyed by `(source, instrument, hour_of_week, computed_from, computed_to)`;
   recomputing the same window replaces its rows, and `latest_spread_stats` reads the most recent
   window.

## Consequences

- Hours with no quotes (the 17:00–18:00 break, weekends) have no rows; consumers must treat them as
  closed, not as zero spread.
- Hourly buckets average a rollover widening that lasts only minutes with the rest of the hour; the
  DQ spread check compares ticks against the hour's p50, so short rollover widening can appear
  there as outliers. Real broker data (DQ-008) will show whether finer buckets are needed.
