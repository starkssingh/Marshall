# ADR 0011 — Data-quality check definitions and thresholds

- **Status:** proposed (completed as DQ-003 and DQ-004 land)
- **Date:** 2026-09-26
- **Tasks:** DQ-001, DQ-002 (DQ-003, DQ-004 append their sections)

## Context

Phase 2 lists what to check and gives default thresholds for some checks. The exact metric of each
check, how it is graded, and thresholds for checks the plan does not cover are decided here.
Thresholds must be fixed before results are seen and change only with an ADR. The human review of
real-data anomalies (DQ-008) will confirm or revise the "proposed" values below.

## Decision — framework

- A check measures one trading day (partition) of one source and returns a number where larger is
  worse. The framework grades it: FAIL if `metric > fail`, WARN if `metric > warn`, else PASS;
  `null` is never reached and a warn of 0 means "any".
- Thresholds, severities and parameters live only in `config/quality.yaml`; every registered check
  must have an entry and every entry must match a check.
- A check that does not apply to a partition (no spread statistics yet, closed day, no tick-rate
  history) produces no result rather than a PASS.
- Where cleaning may drop rows (`DUP_EXACT`, `NONPOSITIVE`, `CROSSED`), the dropped counts are
  added back, so dropping cannot hide a defect from quality.

## Decision — tick checks (DQ-002)

| Check | Metric | Warn | Fail | Source |
| --- | --- | --- | --- | --- |
| `tick.ordering` | share of ticks out of order in their file or at DST-ambiguous/nonexistent times | > 0 | > 0.1% | proposed |
| `tick.duplicates_exact` | share of exact duplicates (incl. dropped) | > 1% | > 20% | proposed |
| `tick.duplicates_diff_price` | share with an earlier tick's timestamp, other quote | > 0.1% | > 1% | plan |
| `tick.nonpositive_crossed` | share non-positive or crossed (incl. dropped) | any | > 0.01% | plan |
| `tick.spread_outliers` | share of usable ticks with spread > 10 × their New York hour-of-week p50 | > 0.1% | > 1% | plan |
| `tick.spikes` | reverting spike events (runs of `SPIKE` ticks) | > 5 | > 50 | plan |
| `tick.stale_quotes` | seconds in London ∪ New York sessions covered by quote-unchanged periods > 120 s | any | > 1800 s | plan |
| `tick.rate_anomalies` | full market hours with tick count < 0.1 × or > 10 × the hour-of-week norm | > 0 | > 3 | proposed |

Notes:

- Ordering: a feed's clock should never run backwards, so any occurrence warns.
- Exact duplicates come from overlapping exports. They are excluded from bars and harmless, so only
  a large share (> 20%, a duplicated import) fails.
- Stale time counts both a frozen feed (repeated quotes) and a silent one (no ticks), measured
  between quote changes inside the sessions; time outside the active sessions is ignored.
- The tick-rate norm is the median tick count for each New York hour of week over the validated
  window. With less than one week of history every hour is its own norm, so the check finds
  nothing; it becomes informative with several weeks of data.

## Decision — bar checks on 1-minute bars (DQ-003)

| Check | Metric | Warn | Fail | Source |
| --- | --- | --- | --- | --- |
| `bar.ohlc_consistency` | bars whose high/low do not bound open and close, any basis | — | any | plan |
| `bar.missing_minutes` | share of whole minutes in London ∪ New York sessions without a bar | > 1% | > 5% | plan |
| `bar.duplicate_starts` | bars sharing a start with an earlier bar | — | any | proposed |
| `bar.extreme_returns` | adjacent-minute mid close-to-close returns beyond 10 robust σ of the day | > 2 | > 10 | proposed |
| `bar.zero_range` | share of session bars whose mid never moved | > 10% | > 50% | proposed |
| `bar.basis_consistency` | ask < bid (close, high or low), mid outside bid/ask, mid open/close ≠ average | — | any | proposed |

Notes:

- Returns are taken only between adjacent minutes; a return across a missing minute is not a
  1-minute return. The robust σ is 1.4826 × the day's median absolute deviation of those returns.
  This is a diagnostic over the whole day, not a feature, so using the full day is acceptable.
  News releases legitimately produce a few extreme minutes, hence the warn level of 2.
- An extreme return is labelled with the start of the bar whose close moved.
- Duplicate starts and bid/ask/mid mismatches cannot come out of the bar builder, so any
  occurrence means a corrupted or foreign bar file and fails.
