# ADR 0018 — Calendar and session columns in datasets

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DS-007 (builds on DATA-002, ADR 0002)

## Context

DS-007 adds columns known in advance: trading day, session flags, minutes since session open,
overlap flag, minutes to the next LBMA auction, the US 08:30 release window and rollover. They must
match the session table and pass the leakage harness like any other feature.

## Decision

1. **Anchored to the decision time.** Columns describe the calendar at the decision time t, so the
   trading day is `trading_day(t)`, not the trading day of the bar that just closed: a decision at
   17:00 New York already belongs to the next trading day.
2. **One source of truth.** Values come from `build_session_table` (DATA-002), which converts
   local session and anchor times to UTC per date. Sessions and overlaps give `in_<name>` and
   `<name>_minutes_since_open`; every configured event anchor gives `minutes_to_<anchor>` and
   `minutes_since_<anchor>`; the day gives `is_open`, `is_early_close`, `is_us_holiday`,
   `is_uk_holiday`, `day_of_week` and `minutes_to_market_close`.
3. **Fixed lookup horizon.** Next and last anchors are looked up at most seven days away. Without
   a cap, a row near the end of a dataset could see a different "next anchor" depending on how far
   the data extends — harmless in meaning but a truncation-invariance violation, and the harness
   would rightly flag it.
4. **Event windows are configuration.** `in_<anchor>_window` covers `[anchor - before,
   anchor + after)` with windows in `config/sessions.yaml` (`event_windows`): US data release
   5 minutes before to 30 minutes after, rollover 15 minutes either side. These are proposed
   defaults fixed before any research result; changing them requires an ADR. The calendar
   configuration is part of the dataset config digest, so a change gives new dataset ids.
5. **Placement.** `xq.datasets.calendar_columns` is a new module beside the plan's `datasets/`
   files; `base.v1` includes its columns. FEAT-006 (Sprint 7) adds engineered time features
   (cyclical hour, one-hot sessions) on top.

## Consequences

- Every base dataset carries the same calendar columns, tested against a brute-force reading of
  the session table and through the leakage harness.
- Until the broker's calendar is confirmed (ADR 0002, ADR 0013 item 5), these columns describe the
  default CME-style calendar.
