# ADR 0006 — Tick cleaning rules, partitions and versioning

- **Status:** accepted; the `SPIKE` definition in §3 is superseded by ADR 0008
- **Date:** 2026-09-26
- **Tasks:** DATA-007 (and the raw-mirror change it required)

## Context

DATA-007 asks for non-destructive cleaning: rules set flag bits, only exact duplicates,
non-positive and crossed quotes may be dropped (per config), every action is logged with original
values, and rules are versioned. Several details are left open: what each rule measures exactly,
over which ticks rolling statistics run, how partitions are cut, and how rules interact with the
live/research parity requirement (development plan, assumption 13).

The project instructions (§20) list the pipeline as Raw → Validation → Cleaning → Normalization.
The plan normalizes timestamps first, because calendar rules (CLOSED_MARKET), session checks and
trading-day partitions need UTC. The plan governs architecture; the order here is Raw →
timestamp normalization (DATA-006) → Cleaning (DATA-007) → Validation (DQ checks).

## Decision

1. **Partitions.** One clean partition per source, instrument, rules version and *trading day*
   (17:00 New York roll), at
   `data/clean/<source>/<instrument>/rules=<version>/year=YYYY/month=MM/day=DD/part.parquet`.
   Ticks are sorted by `(ts_utc, raw_file_id, row_num)` so the result does not depend on read order.
   A partition is rebuilt only when its contributing raw files change, its file changed or `--force`
   is given; rebuilding the same inputs reproduces the same bytes.
2. **Mirror schema v2.** MT5 rows carry only the changed side, so a single UTC day of the raw mirror
   cannot reconstruct the quote on its own. The mirror now also stores each row's canonical view
   (`c_bid`, `c_ask`, `c_bid_size`, `c_ask_size`, `c_flags`, as `to_canonical` defines it over the
   whole file). Its version is recorded in each Parquet part; cleaning refuses older parts and
   `xq rebuild-mirror` regenerates them from the immutable stored copies.
3. **Rules** (all flag; `RULES` in `xq.data.clean`):
   - `NONPOSITIVE` (bid or ask ≤ 0), `CROSSED` (bid > ask; a zero spread is not crossed).
   - `DUP_EXACT`: timestamp, quote and sizes equal to an *earlier* tick of the partition (the first
     occurrence is kept unflagged). `DUP_TS_DIFF_PRICE`: same timestamp as an earlier tick, other
     quote. Overlapping exports therefore show up as `DUP_EXACT`.
   - `CLOSED_MARKET`: outside the calendar's market hours for the trading day (ADR 0002).
   - `SPREAD_OUTLIER`: spread above `multiple` (10) × the median of the previous `window_ticks`
     spreads. It is causal and local; it can fire on legitimate spread widening at the 17:00
     rollover. The hour-of-week comparison belongs to the DQ spread check, which uses DATA-009
     statistics.
   - `SPIKE`: |mid log return| / robust scale > `z_threshold` (8), where the robust scale is
     1.4826 × the median absolute return of the previous `window_ticks` returns (a MAD about zero,
     which is cheap and appropriate because tick returns have a median of about zero), floored at
     `min_scale_bps`. A candidate is confirmed when, within `reversal_ticks` later ticks, the mid
     comes back by at least `reversal_fraction` of the jump; the ticks from the jump up to the
     reversal are flagged. Spread outliers are excluded from spike detection, since a one-sided
     spread blow-out moves the mid without being a price spike.
   - `STALE`: the quote repeats unchanged for more than `stale.seconds` (120). This catches a frozen
     feed that keeps sending the same quote. A feed that sends nothing is a gap, which the DQ
     stale-period check measures from timestamps.
4. **Usable ticks.** Rolling statistics (spread median, return scale, stale timer) run over ticks
   that are known, positive, uncrossed and not exact duplicates, so bad ticks cannot distort them.
5. **Causality.** Every rule except `SPIKE` depends only on the tick and earlier ticks (tested by
   truncation invariance). `SPIKE` needs later ticks to confirm the reversal, so a live system
   cannot know it when the tick arrives. It may describe data, but it must never decide what bars
   contain (DATA-008 configuration rejects it as a bar exclusion).
6. **Drops and logging.** `cleaning.drop` may list only `DUP_EXACT`, `NONPOSITIVE` and `CROSSED`
   (schema-enforced); the default drops nothing. Every rule hit becomes a `cleaning_actions` row
   with the tick's original bid, ask, sizes, `raw_file_id`, `row_num` and rule details. Drops are
   always logged; flag rows can be switched off (`log_flag_actions`) if volume becomes a problem.
7. **Versioning.** `rules_version = "<cleaning.version>-<8 hex>"`, the hash covering the rule
   parameters, `CLEAN_CODE_VERSION` and the mirror schema version. Any change creates a new clean
   store alongside the old one; nothing is overwritten.

## Consequences

- Every rule has an injected-defect test that checks it flags exactly the planted rows and nothing
  else, on dense synthetic ticks where a clean fixture gets no flags.
- `SPREAD_OUTLIER` and `STALE` thresholds are first guesses (matching the DQ thresholds); DQ-008,
  the human review of the top anomalies on real broker data, decides whether they need an ADR'd
  change.
- A change to adapter logic without a mirror schema bump is not detected automatically; rebuild
  the mirror and use `xq clean --force`.
