# ADR 0070 — DQ-008: the owner's calendar decisions (C-35), calendar version s2

- **Status:** accepted
- **Date:** 2026-10-06
- **Decided by:** the project owner (C-35), on proposals C1–C8 of the DQ-008 review
  ([`docs/data/quality-review-2026-10.md`](../data/quality-review-2026-10.md) §4.4, §7.1);
  the schema and its tests implemented by Claude
- **Tasks:** DQ-008, DQ-004, DATA-002, DATA-007, FEAT-001
- **Supersedes:** ADR 0002 decision 3's early-close rules and its "one schedule for every year"
  assumption; ADR 0002's other decisions stand
- **Amends:** ADR 0067 (5) (implemented: feature sets name their calendar), ADR 0057's calendar
  table (the holiday row)
- **Related:** ADR 0069 (thresholds), ADR 0071 (re-export supersession and the exclusion list)

Synthetic data only in this session. Nothing was rebuilt or re-graded on real data; that is the
owner's local session's step.

## Context

Quality run `01M47ZDA2E631VVD7703MXWMQN` compared the calendar with 3,028 pre-vault trading days
of Dukascopy ticks (review §4). The market hours and the three full-close holidays were confirmed
exactly; the holiday early closes were wrong in four ways, one of which — the early-close time
moving from 13:00 to 14:30 New York in 2022 — ADR 0002's single `early_close_time` cannot
express. Those errors account for all 37 `cal.closed_market_ticks` and all 36
`cal.holiday_behaviour` failures and 51 of the 191 `cal.gap_location` failures.

## Decision

1. **C1 approved — market hours unchanged.** `market.tz` New York, `open` 18:00, `close` 17:00,
   `week_open` 18:00, Monday to Friday trading days.
2. **C2 approved — full closes unchanged.** New Year's Day, Good Friday and Christmas Day
   (prefix match, observed dates included).
3. **C3 approved — date-dependent early closes (a schema change).** Every other US-calendar
   holiday closes at **13:00 New York through 2021-12-31** and at **14:30 New York from
   2022-01-01**. ADR 0002's "one schedule for every year" is superseded.
4. **C4 approved.** 12-31 is a **full trading day**: removed from `early_close_dates`.
5. **C5 approved.** 12-24 stays an early close, at **13:45** New York (every year).
6. **C6 approved.** The **day after Thanksgiving** is an early close at **13:45** New York.
7. **C7 approved — named exceptions.** The National Days of Mourning (2018-12-05, 2025-01-09)
   are **full trading days**, configured by name (`full_days: [National Day of Mourning]`,
   prefix match on the US calendar's holiday name). Every other NYSE holiday keeps its configured
   treatment; the NYSE calendar stays the authority for the dates.
8. **C8 approved.** The irregular dates (2019-07-04 to 16:56, 2019-09-02 to 14:17, 2024-07-04 to
   13:59, MLK Day 2014 to 11:59 and 2017 to 12:30) are **not modelled**; the calendar checks keep
   flagging them.
9. **A changed calendar is a new feature-set version (ADR 0067 (5)).** The calendar now has a
   version: **`s1`** (ADR 0002's rules) is replaced by **`s2`** (this decision). `core.v1`, bound to
   `s1`, is **retired before it was ever built on real data**; **`core.v2`** has exactly its
   features and parameters, on `s2`, and `ds_core` names it. `ds_base` stays on `base.v1` (built
   in code; its calendar columns follow the configured calendar and its dataset id already covers
   it through the config digest).

## Schema (`config/sessions.yaml`, `xq.core.config`)

- `version` (required): the calendar version. Any change to `market` or `holidays` needs a new
  one.
- A **time schedule** (`TimeSchedule`) is a bare `"HH:MM"` (one time for every date) or a list of
  `{since, time}` entries — the first without `since`, the rest with strictly increasing `since`
  dates; `time_on(schedule, day)` gives the time of the latest entry starting on or before the
  date. It is used by:
  - `holidays.early_close_time` — `[{time: "13:00"}, {since: "2022-01-01", time: "14:30"}]`;
  - `holidays.early_close_dates` — now a mapping `MM-DD -> schedule` (`"12-24": "13:45"`);
  - `holidays.early_close_after` — the trading day after a named US-calendar holiday
    (`Thanksgiving Day: "13:45"`).
- `holidays.full_days`: named US-calendar holidays the market trades through in full. A name
  both closed and full is a configuration error.
- When several early-close rules apply to one day, the earliest close wins
  (`MarketCalendar.early_close`). A full-day holiday keeps its `holiday` name in the session table,
  so event anchors with `skip_on: [us_holiday]` (COMEX open, US data release) are still skipped
  on it; only the market hours change.
- **The clean rules version now includes the calendar version** (`rules_version(cleaning,
  calendar)`, `clean_rules_version(cfg)`): `CLOSED_MARKET` depends on the calendar, so a new
  calendar is a new clean store (and new bar builds) rather than a silently stale one. With
  ADR 0069's `c2` the label is `c2-<hash>`.
- `FeatureSetConfig.calendar`: the calendar version a configured feature set is computed on;
  `resolve_feature_set` refuses a set that names no calendar or another one
  (`FeatureSetCalendarError`). `core.v2` repeats `core.v1` through a YAML merge key, so the two
  definitions cannot drift apart.

## Consequences

- On real data the clean store, the bars and the quality run are rebuilt under `s2` and `c2`; the
  calendar failures listed in the context should disappear except the C8 dates, and the three
  days after Thanksgiving the review set aside (2019-11-29, 2020-11-27, 2021-11-26) stop failing
  `cal.missing_open_data`. Not yet measured: nothing ran on real data in this session.
- `cal.closed_market_ticks` and `cal.holiday_behaviour` keep their thresholds (ADR 0069, T4/T6)
  and are judged only after that re-grading.
- Any later calendar change (a broker's published calendar, say) is a new calendar version, a new
  clean store, and a new version of every feature set computed on it.
- Known truth (`tests/unit/data/test_calendar_decisions.py`): 13:00 closes through 2021 and 14:30
  from 2022 on MLK Day, Independence Day and Thanksgiving; 12-31 full days in the seven reviewed
  years; 12-24 at 13:45; the day after Thanksgiving at 13:45; the two days of mourning full days
  with their names kept; market hours and full closes unchanged; on 2025-01-20 only ticks from
  14:30 New York are `CLOSED_MARKET`; malformed schedules, a holiday both closed and full, and a
  calendar without a version are refused. `tests/unit/features/test_configured_sets.py`: `core.v1`
  is refused on `s2` and `core.v2` repeats its features.
