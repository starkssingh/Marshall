# ADR 0002 — Market calendar and session defaults

- **Status:** accepted (defaults to be confirmed against the execution broker)
- **Date:** 2026-09-26
- **Tasks:** DATA-002

## Context

Spot XAUUSD trades OTC almost around the clock, but every venue has a daily break, a weekend
closure, holiday closures and early closes, and these differ between brokers. The execution broker
is not yet chosen (development plan, section 1). Session features, quality checks (DQ-004), cost
model blackouts (BT-008) and the vault boundary all depend on one calendar.

The plan states "weekly open Sunday 17:00 New York" and "daily break around 17:00–18:00 New York".
Read together, 17:00 is the boundary of the trading week and of each trading day, while quotes
resume after the break.

## Decision

1. **Trading day.** Trading day D covers `[17:00 New York on D-1, 17:00 New York on D)`. This is a
   fixed convention in code (`xq.core.time`), not configuration.
2. **Market hours** (`config/sessions.yaml`, `market`): quotes from 18:00 New York on D-1
   (`open`; `week_open` for Monday, i.e. Sunday 18:00) to 17:00 New York on D (`close`), Monday to
   Friday trading days. Local times are placed on whichever calendar date puts them inside the
   trading day, so the rules also work for venues configured in another time zone.
3. **Holidays.** US dates come from the `holidays` package's NYSE financial calendar (observed
   dates and one-off closures included); English bank holidays from its GB/ENG calendar.
   - Closed all day: New Year's Day, Good Friday, Christmas Day (prefix match, so "(observed)"
     dates are included).
   - Every other NYSE holiday: early close at 13:30 New York. 24 and 31 December also close early.
   - English bank holidays do not close the market; they remove the LBMA auction anchors.
4. **Sessions** are defined in local time on the trading day's calendar date (Tokyo 09:00–18:00,
   London 08:00–17:00, New York 08:00–17:00), converted to UTC per date and clipped to market
   hours. Overlaps are intersections of clipped sessions.
5. **Event anchors:** LBMA 10:30 and 15:00 London (skipped on English bank holidays; no PM auction
   on 24/31 December), COMEX open 08:20 and US data 08:30 New York (skipped on US holidays),
   rollover 17:00 New York (kept on early-close days, since financing still rolls).
6. **YAML local times must be quoted strings.** YAML 1.1 reads an unquoted `17:00` as the integer
   1020, which would silently become 00:17; the schema rejects anything but `"HH:MM"` strings.

## Consequences

- The calendar is explicit and testable, and DST is handled by construction.
- The defaults are CME-style approximations of a retail CFD calendar. Once the broker is named,
  its published trading hours and holiday schedule replace them (a config change plus a note in
  this ADR's successor), and DQ-004 must confirm them against real ticks: no data while closed,
  data while open, weekly gap at the expected UTC hour.
- NYSE one-off closures (for example national days of mourning) are treated as early closes, not
  full closures; DQ-004 will show whether the broker agrees.
