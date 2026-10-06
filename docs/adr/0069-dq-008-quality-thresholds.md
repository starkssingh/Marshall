# ADR 0069 — DQ-008: the one allowed change to the data-quality thresholds

- **Status:** **proposed** — a draft for the owner's decision. Nothing in `config/quality.yaml` or
  `config/base.yaml` has been changed. This ADR is not accepted and must not be cited as a decision.
- **Date:** 2026-10-06
- **Proposed by:** Claude, from the DQ-008 review in
  [`docs/data/quality-review-2026-10.md`](../data/quality-review-2026-10.md)
- **Decided by:** the project owner (pending)
- **Tasks:** DQ-008
- **Refines:** ADR 0011 (check definitions and thresholds), ADR 0013 item 1 (the thresholds may
  change **once**, through an ADR, after the DQ-008 human review of a quality report on real broker
  data, and **never** after any strategy result exists), ADR 0013 item 2 (spread buckets)

## Context

Quality run `01M47ZDA2E631VVD7703MXWMQN` graded 3,028 pre-vault trading days of real Dukascopy
XAUUSD ticks (504,272,444 ticks, 2014-01-02 … 2025-09-25) and returned 44,940 pass, 3,341 warn and
3,285 fail. The DQ-008 review attributes every failure to one of three causes: a wrong holiday
calendar, whole missing export hours, or a threshold calibrated for a different kind of feed.

Eight of the eighteen checks never fire in twelve years, and two (`bar.extreme_returns`,
`tick.spread_outliers`) fire only on days a gold researcher would name from memory — the 08:30 New
York release minutes, the FOMC statement, and 2020-03-24. Those ten need no change.

Two checks do not discriminate on this feed:

- `tick.spikes` fails on **1,652 of 3,028 days (55 %)** and passes on 131. In 2018, 254 of 258 days
  fail and none passes. The typical flagged event is a **~1 bp mid move that reverts within five
  ticks** (median absolute jump 0.135–0.465 USD by year); 841,729 ticks of 504 million carry the
  `SPIKE` flag. The plan's levels (warn 5, fail 50 reverting spikes a day) were written for a broker
  feed; this feed's rate rises from ~80,000 to ~245,000 ticks a day across the window, so an
  absolute per-day count is not comparable across years either.
- `tick.stale_quotes` fails on **651 days**, but the `STALE` cleaning flag — which marks a genuinely
  unchanged quote — fires on **9 ticks in 504 million**. The metric counts time between quote
  *changes*, so an hour with no ticks at all scores the same as a frozen feed. On 625 of the 651
  failing days the longest run is at least 55 minutes, and 574 of those are exactly 60 minutes:
  these are the missing export hours that `bar.missing_minutes` and `cal.missing_open_data` already
  report, counted a third time.

This ADR is the one change ADR 0013 item 1 allows. Because it is the only one, it should cover
every threshold that needs to move, together.

## Proposed decision

### 1. `tick.spikes`: grade per million ticks, not per day

Change the metric's unit from "reverting spike events per day" to **"reverting spike events per
million usable ticks of the day"**, and set:

| | Now | Proposed |
| --- | --- | --- |
| `unit` | events per day | events per million ticks |
| `warn` | 5 | **2000** |
| `fail` | 50 | **4000** |

Grading the twelve years under these levels gives:

| Year | Days | Pass | Warn | Fail |
| --- | --- | --- | --- | --- |
| 2014 | 258 | 256 | 2 | 0 |
| 2015 | 258 | 256 | 2 | 0 |
| 2016 | 258 | 252 | 5 | 1 |
| 2017 | 257 | 255 | 2 | 0 |
| 2018 | 258 | 149 | 107 | 2 |
| 2019 | 258 | 248 | 10 | 0 |
| 2020 | 259 | 255 | 4 | 0 |
| 2021 | 258 | 258 | 0 | 0 |
| 2022 | 258 | 258 | 0 | 0 |
| 2023 | 257 | 257 | 0 | 0 |
| 2024 | 259 | 259 | 0 | 0 |
| 2025 | 190 | 190 | 0 | 0 |
| **Total** | 3,028 | 2,893 | 132 | **3** |

0.1 % of days fail and 4.4 % warn, against 55 % and 41 % today. The residual concentration in 2018
is deliberate and is the honest answer: 2018's tick stream really is the jumpiest in the window
(median 1,844 spike events per million ticks against 115 in 2014), so the check should say so
rather than be tuned until it does not.

A documented consequence: the 2020-03-24/25 disorderly-market days stop failing **this** check
(their spike count per million ticks is below the warn level because their tick count is very high).
They continue to fail `tick.spread_outliers` at 47.6 % and 24.2 % of ticks, which is the check that
should own that defect.

An alternative considered and rejected: keep the per-day unit and raise the levels. It cannot work —
the same absolute count means something different at 80,000 and at 245,000 ticks a day.

### 2. `cleaning.spike.min_scale_bps`: raise the floor on the robust return scale

Currently `0.05` bp (`config/base.yaml`, `cleaning.spike`). With `z_threshold: 8.0`, the smallest
move the rule can call a spike is 8 × 0.05 = **0.4 bp**, which at 4,000 USD gold is 1.6 cents. In a
quiet window the floor, not the 8-sigma test, sets the bar, which is why the rule flags ordinary
bid/ask bounce.

Proposed: raise `min_scale_bps` so that the smallest flaggable spike is economically meaningful —
**0.125 bp**, which with the unchanged `z_threshold: 8.0` makes the floor 1 bp (≈ 0.40 USD at
4,000 USD, ≈ 0.13 USD at 1,300 USD).

This is a **cleaning-rule parameter, not a quality threshold**. Changing it produces a new clean
rules version, so `xq clean` and `xq build-bars` must be re-run over the whole history (about
21 minutes together, measured) and the quality run repeated. Its effect on the `SPIKE` flag count
has therefore **not** been measured — the review observed only that the median flagged jump is about
1 bp, so a 1 bp floor should remove most of the bulk while leaving the tail. If the owner prefers to
avoid a clean-store rebuild, item 1 alone fixes the grading and this item can be dropped; the
`SPIKE` flag never enters bar prices (it is excluded from `bars.exclude_flags` as non-causal,
ADR 0007), so its count affects only the quality grade.

### 3. `tick.stale_quotes`: stop counting silence as staleness

Two options; the review recommends (a).

**(a) Rule change (recommended).** Measure stale time only across intervals in which the feed
actually produced ticks: an interval with **no ticks at all** is missing data and belongs to
`bar.missing_minutes` and `cal.missing_open_data`, not here. Thresholds unchanged (warn any, fail
1,800 s). On this history the check would then report essentially nothing, which is the correct
answer: 9 stale ticks in 504 million.

**(b) Threshold change only.** Keep the metric and demote the check from `major` to `minor`, so a
missing hour no longer raises a major failure that DQ-007 blocks on. This leaves the triple-counting
in place.

Option (a) changes `src/xq/quality/checks/ticks.py` and its tests, not just configuration. Either
way the signal is not lost: the same days still fail `bar.missing_minutes`.

### 4. Everything else: unchanged

- `bar.missing_minutes`, `cal.missing_open_data`, `cal.gap_location`, `tick.rate_anomalies` keep
  their thresholds. They correctly report a real defect — 1,203 whole missing hours, 1.81 % of the
  pre-vault market minutes. The remedy is a better export and an exclusion list (review §7.3, §7.5),
  not a looser threshold.
- `bar.extreme_returns` and `tick.spread_outliers` keep their thresholds (review §6.3).
- The eight checks that never fire keep their thresholds (review §2).
- `cal.closed_market_ticks` and `cal.holiday_behaviour` keep their thresholds. Their 37 and 36
  failures are entirely the calendar's fault; they must be re-graded after the calendar changes
  (review §7.1) and only then judged.

### 5. ADR 0013 item 2: spread buckets stay hourly

The review measured how much a New York hour-of-week bucket hides of the rollover widening: across
twelve years the worst 15-minute median inside the 16:00 hour is ×1.06 to ×1.23 of the hour's
median, and inside the 18:00 reopen hour ×1.07 to ×1.22. Against a check that fires at **10×** the
bucket median this is immaterial, and the `SPREAD_OUTLIER` cleaning flag fired on 1,079 of
504,272,444 ticks (0.0002 %).

Proposed: keep hourly buckets for both the spread statistics (DATA-009) and
`tick.spread_outliers`, and close the known issue "the `SPREAD_OUTLIER` cleaning flag fires on
rollover widening" as not observed in the data. The rollover remains modelled where it belongs —
the `rollover` event window 16:45–18:15 New York (ADR 0026), which the measurement confirms is the
right window, and the cost model's rollover multiplier.

## Consequences if accepted

- `config/quality.yaml` changes for item 1 (and item 3b if chosen); `config/base.yaml`
  `cleaning.spike.min_scale_bps` changes for item 2; `src/xq/quality/checks/ticks.py` changes for
  item 1's unit and for item 3a, each with tests.
- Item 2 creates a new clean rules version: `xq clean` and `xq build-bars` are re-run over the whole
  history and every bar set's sha256 changes. No dataset has been built on real data, so nothing
  downstream is invalidated.
- The quality run is repeated after the calendar changes (review §7.1) and these threshold changes,
  and that run — not `01M47ZDA2E631VVD7703MXWMQN` — becomes the one DQ-007 gates datasets on.
- **The allowance in ADR 0013 item 1 is then spent.** No further threshold change is possible
  without superseding that decision, and none is possible at all once the first strategy result is
  recorded.

## Honesty note

These levels were chosen after seeing the real distribution of the metrics, which is what ADR 0013
item 1 exists to permit, once. They are **not** informed by any strategy result: none exists, no
dataset has been built on real data, no model has been trained and no EDA, returns analysis or
backtest has been run on this data. The judgement that a ~1 bp reverting tick move is market
microstructure rather than a data defect is a judgement from the magnitude distribution, not a
measurement. If the owner disagrees with that judgement, the correct response is to keep
`tick.spikes` as it is and accept that it fails on 55 % of days, with DQ-007 exclusions recorded
accordingly — not to adjust the levels again later.
