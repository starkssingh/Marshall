# ADR 0003 — Source clock conventions and UTC normalization

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DATA-003, DATA-006

## Context

MT4/MT5 exports are written in broker "server time", commonly UTC+2 in winter and UTC+3 in
summer so that the server's midnight is the 17:00 New York rollover. That clock changes offset on
the **US** DST dates. Brokers often describe it as "EET" or "GMT+2/+3", but `Europe/Athens` changes
on the **EU** dates, so for two to three weeks in March and one week around the end of October
the two disagree by an hour. Misreading the clock shifts every session feature and every bar
boundary (development plan, assumption 2).

Around DST changes, local wall times can be repeated (fall back) or skipped (spring forward).

## Decision

1. Every source declares a clock convention in config; none is assumed. Supported forms:
   `UTC`, a fixed offset `UTC±HH:MM`, an IANA zone `tz:<zone>`, and `NY±N` — New York wall time
   shifted by N hours. The usual MT5 broker server clock is `NY+7`.
2. A source's clock convention is part of its identity (`data_sources.clock_convention`). A
   different convention requires a new source id (enforced at ingest in DATA-004).
3. Conversion is deterministic and non-destructive:
   - repeated wall times are read as the first occurrence until the file's clock jumps backwards
     within that repeated hour, then as the second occurrence; the rows are flagged
     `TS_DST_AMBIGUOUS`;
   - skipped wall times are shifted forward to the transition instant and flagged
     `TS_DST_NONEXISTENT`;
   - rows earlier than a preceding row of the same file are flagged `TS_OUT_OF_ORDER`; canonical
     ticks are stably sorted by UTC time.
   No row is dropped by normalization. For XAUUSD with a `NY+7` clock the repeated and skipped
   hours fall on Sunday mornings, when the market is closed, so any flagged row is itself a
   data-quality signal.
4. The declared convention is verified against the data: tests locate the weekly open, the weekly
   close and the daily 17:00 New York rollover gap at the expected UTC hours on both sides of each
   US DST change, and show that declaring `tz:Europe/Athens` for a `NY+7` feed puts them an hour
   off in the mismatch weeks. DQ-004 runs the same check on real data.
5. Sprint 1 implements DATA-006 before DATA-003 and DATA-004 (the plan lists DATA-003, DATA-004,
   DATA-006): the adapter's `to_canonical` and the raw-store manifest (`first_ts_utc`,
   `last_ts_utc`, UTC-day partitions of the Parquet mirror) both need UTC conversion.

## Consequences

Clock mistakes surface as failing gap-location checks instead of silently shifted features.
Adding a source with an unusual clock needs only a new convention string, or a new convention kind
with tests.
