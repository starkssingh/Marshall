# DQ-008 — human review of the first real-data quality report

- **Date:** 2026-10-06
- **Reviewer:** Claude, in a real-data session on the owner's Mac (ADR 0062), under the owner's
  instruction for this session
- **Task:** DQ-008 (plan Phase 2; ADR 0011 defines the checks, ADR 0013 item 1 allows the
  thresholds to change **once** after this review and never after a strategy result exists)
- **Subject:** quality run `01M47ZDA2E631VVD7703MXWMQN`,
  `reports/quality/01M47ZDA2E631VVD7703MXWMQN/report.md`
- **Data:** Dukascopy XAUUSD ticks, 143 monthly dukascopy-node CSVs (2014-01 … 2025-11),
  520,973,737 ticks; graded window 2014-01-02 … 2025-09-25 (3,028 trading days, 504,272,444
  ticks), **pre-vault only** — `--include-vault` was never used and no vault day was read by any
  analysis in this document
- **Code:** git sha `a8f6ca9c97e14676f7d04b0cd16e6cd076adc53b`, config hash `137bd61d119d1dc5`,
  clean rules `c1-9215d40e`, bar build `b1-95614b4b`
- **Status of this document:** findings and **proposals only**. No threshold, no calendar value
  and no exclusion list was changed. Nothing in it derives from a strategy result — none exists.

## 0. Scope and what was run

| Step | Command | Runtime | Result |
| --- | --- | --- | --- |
| Pre-ingest sanity check | two read-only passes over the 143 CSVs | 63 s | every file structurally sound (§1) |
| Ingest | `xq ingest --source dukascopy --path data/downloads/csv` | 24 m 51 s | 142 ingested, 1 skipped (March 2024, same SHA-256), 518,240,161 rows |
| Raw verification | `xq verify-raw` + an independent re-hash of both copies of all 143 months | 17 s + 33 s | every raw file matches its manifest, mode 0444; CSV = raw copy = manifest SHA-256 |
| Clean | `xq clean --source dukascopy` | 16 m 39 s | 3,056 trading days, 518,375,972 ticks, 1,029,287 flagged, **0 dropped** |
| Bars | `xq build-bars --source dukascopy` | 4 m 35 s | 144 months; 1m 4,134,802 · 5m 827,430 · 15m 275,847 · 30m 137,953 · 1h 69,006 · 4h 18,410 · 1d 3,075 |
| Spread statistics | `xq spread-stats --source dukascopy` | 1 m 21 s | 115 hours of week from 504,272,444 ticks, cut at `vault.start` |
| Validate | `xq validate --source dukascopy` | 3 m 48 s | 3,028 days; **44,940 pass, 3,341 warn, 3,285 fail** |

No step failed and no step was re-run. After the owner's instruction, the 143 downloaded CSVs were
deleted once verified byte-identical to the raw store; each deletion is logged with size and
SHA-256 in `data/deleted_csvs.log`. **`data/raw` is now the only copy of this market data.**

## 1. Pre-ingest sanity check of the CSVs

**Observed.** All 143 files: header exactly `timestamp,askPrice,bidPrice,askVolume,bidVolume`;
UTC millisecond timestamps; strictly increasing within every file (0 inversions, 0 ties); every
timestamp inside its own month in UTC; no NaN, non-positive or crossed quote; prices tracking the
real gold path.

| Year | Months | GB | Ticks (M) | Ask min | Ask max | Median spread USD | Market minutes inside gaps > 10 min |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2014 | 12 | 0.96 | 20.6 | 1132.2 | 1389.6 | 0.295 | 18,229 |
| 2015 | 12 | 1.23 | 25.7 | 1046.6 | 1307.9 | 0.303 | 1,672 |
| 2016 | 12 | 2.20 | 46.2 | 1062.1 | 1375.4 | 0.303 | 380 |
| 2017 | 12 | 2.18 | 45.7 | 1146.3 | 1357.7 | 0.240 | 1,076 |
| 2018 | 12 | 1.67 | 35.2 | 1160.4 | 1366.2 | 0.231 | 8,358 |
| 2019 | 12 | 1.69 | 35.9 | 1266.5 | 1557.3 | 0.292 | 10,152 |
| 2020 | 12 | 2.54 | 53.3 | 1451.5 | 2075.9 | 0.402 | 16,156 |
| 2021 | 12 | 2.44 | 51.2 | 1675.7 | 1959.6 | 0.351 | 14,316 |
| 2022 | 12 | 2.57 | 53.6 | 1615.1 | 2070.8 | 0.362 | 2,212 |
| 2023 | 12 | 1.73 | 36.0 | 1804.9 | 2146.9 | 0.334 | 4,339 |
| 2024 | 12 | 2.61 | 54.5 | 1984.5 | 2790.3 | 0.389 | 11,245 |
| 2025 | 11 | 3.04 | 63.3 | 2615.0 | 4381.8 | 0.577 | 942 |

**Evidence.** No month is truncated at the file level: every month's first tick is at the weekly or
daily open of its first trading day and its last tick at the close of its last. Four months carry a
hole at a month boundary, which is why they looked short: 2015-07 (165 market minutes missing at
the start), 2024-01 (61), 2018-07 (60, the 23:00 UTC hour of 31 July), 2021-06 (60).

**Interpretation.** The dukascopy-node CSV route produced structurally clean files. The only defect
class is missing hours (§5).

**Limitations.** The sanity check reads the vendor's own numbers; it cannot detect a quote the
vendor reported wrongly.

**Action.** None. The files were fit to ingest and were ingested.

## 2. Pass / warn / fail per check per year

Cells are `pass/warn/fail` over that year's graded trading days.

| Check | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `tick.ordering` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.duplicates_exact` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.duplicates_diff_price` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.nonpositive_crossed` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.spread_outliers` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 249/5/**5** | 257/1/0 | 258/0/0 | 257/0/0 | 257/2/0 | 183/7/0 |
| `tick.spikes` | 64/191/**3** | 2/115/**141** | 14/100/**144** | 34/41/**182** | 0/4/**254** | 0/21/**237** | 0/117/**142** | 0/119/**139** | 3/148/**107** | 10/231/**16** | 4/72/**183** | 0/86/**104** |
| `tick.stale_quotes` | 153/11/**94** | 239/5/**14** | 250/6/**2** | 243/4/**10** | 175/12/**71** | 144/35/**79** | 135/1/**123** | 143/4/**111** | 237/3/**18** | 213/6/**38** | 176/0/**83** | 181/1/**8** |
| `tick.rate_anomalies` | 100/142/**16** | 239/19/0 | 256/2/0 | 249/8/0 | 155/101/**2** | 135/121/**2** | 101/150/**8** | 107/146/**5** | 226/32/0 | 208/47/**2** | 136/121/**2** | 180/10/0 |
| `bar.ohlc_consistency` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `bar.duplicate_starts` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `bar.basis_consistency` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `bar.zero_range` | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `bar.missing_minutes` | 160/6/**92** | 240/4/**14** | 251/6/**1** | 245/4/**8** | 184/6/**68** | 175/6/**77** | 135/5/**119** | 147/6/**105** | 240/1/**17** | 218/3/**36** | 176/0/**83** | 181/2/**7** |
| `bar.extreme_returns` | 151/103/**4** | 149/103/**6** | 194/60/**4** | 199/56/**2** | 225/33/0 | 194/60/**4** | 226/32/**1** | 206/47/**5** | 211/44/**3** | 207/49/**1** | 217/41/**1** | 179/11/0 |
| `cal.closed_market_ticks` | 256/0/**2** | 256/1/**1** | 258/0/0 | 257/0/0 | 255/0/**3** | 254/2/**2** | 257/0/**2** | 257/0/**1** | 252/0/**6** | 252/0/**5** | 250/0/**9** | 184/0/**6** |
| `cal.holiday_behaviour` | 6/1/**1** | 6/1/**1** | 6/0/0 | 6/0/0 | 6/0/**3** | 4/2/**2** | 6/0/**2** | 6/0/**1** | 1/0/**6** | 2/0/**5** | 0/0/**9** | 1/0/**6** |
| `cal.missing_open_data` | 188/57/**13** | 251/7/0 | 257/1/0 | 251/6/0 | 230/28/0 | 212/45/**1** | 187/70/**2** | 191/66/**1** | 255/3/0 | 241/15/**1** | 208/51/0 | 189/1/0 |
| `cal.gap_location` | 210/6/**42** | 247/5/**6** | 251/6/**1** | 248/5/**4** | 232/6/**20** | 235/4/**19** | 230/5/**24** | 233/6/**19** | 249/1/**8** | 244/2/**11** | 226/2/**31** | 183/1/**6** |

**Observed.** Eight of the eighteen checks never fail and never warn in twelve years:
`tick.ordering`, `tick.duplicates_exact`, `tick.duplicates_diff_price`,
`tick.nonpositive_crossed`, `bar.ohlc_consistency`, `bar.duplicate_starts`,
`bar.basis_consistency`, `bar.zero_range`.

**Evidence.** Flag totals over the 504,272,444 graded ticks:

| Flag | Ticks | Share |
| --- | --- | --- |
| `TS_DST_AMBIGUOUS`, `TS_DST_NONEXISTENT`, `TS_OUT_OF_ORDER`, `MISSING_QUOTE` | 0 | 0 |
| `DUP_EXACT`, `DUP_TS_DIFF_PRICE`, `NONPOSITIVE`, `CROSSED` | 0 | 0 |
| `SPREAD_OUTLIER` | 1,079 | 0.00021 % |
| `SPIKE` | 841,729 | 0.16692 % |
| `CLOSED_MARKET` | 140,741 | 0.02791 % |
| `STALE` | **9** | 0.0000018 % |

**Interpretation.** The feed is structurally pristine: no clock reversal, no DST artefact, no
duplicate, no crossed or non-positive quote, and the bar builder's invariants hold on every one of
the 3,028 days. Every failure in the report is one of three things — a wrong calendar (§4), a
missing export hour (§5), or a check threshold that does not suit this feed (§6).

**Limitations.** Zero duplicates is expected rather than reassuring: only one Dukascopy format was
ingested, and ingesting `.bi5` for the same period would duplicate every tick (ADR 0057).
`tick.rate_anomalies` needs four weeks of history per hour of week, so the first weeks of 2014 are
graded against a thin norm.

**Action.** None for these eight checks. They are doing their job and have nothing to report.

## 3. Top 20 anomalies per failing check

The full tables, as produced from `quality_results.details_json`, are reproduced here. Values are
the check's own units (see `config/quality.yaml`).

### `tick.spikes` — 1,652 failing days

| Trading day | Timestamp (UTC) | Mid jump USD | Day metric (events) |
| --- | --- | --- | --- |
| 2020-03-24 | 2020-03-24 12:13:37.387 | 14.12 | 314 |
| 2020-03-24 | 2020-03-24 11:26:28.732 | −9.048 | 314 |
| 2020-03-25 | 2020-03-25 00:45:08.929 | −9.013 | 291 |
| 2020-03-25 | 2020-03-25 00:45:11.876 | 8.561 | 291 |
| 2020-08-03 | 2020-08-02 22:00:21.878 | 8.540 | 67 |
| 2020-03-25 | 2020-03-25 00:46:36.856 | 8.282 | 291 |
| 2020-03-24 | 2020-03-24 11:51:12.448 | 8.277 | 314 |
| 2020-03-25 | 2020-03-25 00:46:34.405 | 7.992 | 291 |
| 2020-03-25 | 2020-03-25 00:46:21.226 | 7.578 | 291 |
| 2020-03-25 | 2020-03-25 00:46:31.312 | 7.513 | 291 |
| 2025-04-07 | 2025-04-06 22:07:03.141 | −7.475 | 536 |
| 2020-03-25 | 2020-03-25 00:45:59.626 | −7.195 | 291 |
| 2020-03-24 | 2020-03-24 12:11:50.012 | −7.184 | 314 |
| 2020-03-25 | 2020-03-25 00:45:10.284 | 6.847 | 291 |
| 2020-03-24 | 2020-03-24 11:35:49.401 | −6.421 | 314 |
| 2020-03-24 | 2020-03-24 19:43:41.644 | −6.312 | 314 |
| 2020-03-24 | 2020-03-24 09:57:15.793 | −6.191 | 314 |
| 2020-03-24 | 2020-03-24 12:11:23.751 | −6.156 | 314 |
| 2020-03-24 | 2020-03-24 12:14:27.676 | 6.017 | 314 |
| 2020-03-24 | 2020-03-24 09:57:09.804 | −5.990 | 314 |

### `tick.stale_quotes` — 651 failing days

| Trading day | Timestamp (UTC) | Seconds without change | Day metric (s) |
| --- | --- | --- | --- |
| 2023-11-24 | 2023-11-24 17:43:57.224 | 15,360 | 15,360 |
| 2014-10-13 | 2014-10-13 16:59:58.397 | 14,400 | 28,810 |
| 2014-10-20 | 2014-10-20 16:59:59.514 | 14,400 | 25,200 |
| 2022-11-25 | 2022-11-25 18:43:55.367 | 11,760 | 11,760 |
| 2021-11-26 | 2021-11-26 18:43:59.260 | 11,760 | 11,760 |
| 2018-11-23 | 2018-11-23 18:44:30.809 | 11,730 | 15,330 |
| 2017-11-24 | 2017-11-24 18:44:51.395 | 11,710 | 11,710 |
| 2016-11-25 | 2016-11-25 18:44:54.083 | 11,710 | 11,710 |
| 2014-11-28 | 2014-11-28 18:44:58.682 | 11,700 | 15,310 |
| 2020-11-27 | 2020-11-27 18:44:58.823 | 11,700 | 15,300 |
| 2015-11-27 | 2015-11-27 18:45:00.455 | 11,700 | 11,700 |
| 2019-11-29 | 2019-11-29 19:05:39.462 | 10,460 | 10,650 |
| 2024-11-29 | 2024-11-29 19:43:59.187 | 8,161 | 8,161 |
| 2014-09-03 | 2014-09-03 17:59:48.042 | 7,212 | 10,820 |
| 2017-01-24 | 2017-01-24 13:59:53.786 | 7,206 | 7,206 |
| 2020-12-24 | 2020-12-24 12:59:54.868 | 7,205 | 7,205 |
| 2014-10-28 | 2014-10-28 13:59:59.931 | 7,204 | 21,610 |
| 2020-07-29 | 2020-07-29 17:59:56.422 | 7,204 | 7,204 |
| 2014-10-29 | 2014-10-29 08:59:57.687 | 7,203 | 14,410 |
| 2020-12-14 | 2020-12-14 16:59:58.224 | 7,202 | 10,800 |

### `bar.missing_minutes` — 627 failing days

| Trading day | Timestamp (UTC) | Consecutive missing minutes | Day metric (share) |
| --- | --- | --- | --- |
| 2023-11-24 | 2023-11-24 17:44 | 256 | 0.3048 |
| 2014-10-20 | 2014-10-20 17:00 | 240 | 0.5000 |
| 2014-10-13 | 2014-10-13 17:00 | 240 | 0.5714 |
| 2021-11-26 | 2021-11-26 18:44 | 196 | 0.2333 |
| 2022-11-25 | 2022-11-25 18:44 | 196 | 0.2333 |
| 2018-11-23 | 2018-11-23 18:45 | 195 | 0.3036 |
| 2014-11-28 | 2014-11-28 18:45 | 195 | 0.3036 |
| 2016-11-25 | 2016-11-25 18:45 | 195 | 0.2321 |
| 2020-11-27 | 2020-11-27 18:45 | 195 | 0.3036 |
| 2017-11-24 | 2017-11-24 18:45 | 195 | 0.2321 |
| 2015-11-27 | 2015-11-27 18:45 | 195 | 0.2321 |
| 2023-12-28 | 2023-12-28 09:00 | 120 | 0.2143 |
| 2014-09-03 | 2014-09-03 18:00 | 120 | 0.2143 |
| 2014-10-31 | 2014-10-31 12:00 | 120 | 0.4615 |

(14 distinct anomalies of 120 minutes or more; the remainder of the top 20 are further
120-minute runs on 2014-10 and 2020 dates.)

### `cal.gap_location` — 191 failing days

| Trading day | Timestamp (UTC) | Minutes from the calendar boundary | Cause (§4, §5) |
| --- | --- | --- | --- |
| 2023-11-24 | 2023-11-24 17:43:57.224 | −256.0 (close) | day after Thanksgiving |
| 2014-08-25 | 2014-08-25 02:00:40.748 | +240.7 (open) | export hole |
| 2014-10-13 | 2014-10-13 16:59:58.397 | −240.0 (close) | export hole |
| 2014-10-20 | 2014-10-20 16:59:59.514 | −240.0 (close) | export hole |
| 2021-12-31 | 2021-12-31 21:59:58.279 | 210.0 (close) | 12-31 is not an early close |
| 2020-12-31 | 2020-12-31 21:59:58.242 | 210.0 (close) | 12-31 is not an early close |
| 2025-01-09 | 2025-01-09 21:59:58.240 | 210.0 (close) | National Day of Mourning |
| 2019-12-31 | 2019-12-31 21:59:57.987 | 210.0 (close) | 12-31 is not an early close |
| 2015-12-31 | 2015-12-31 21:59:53.801 | 209.9 (close) | 12-31 is not an early close |
| 2018-12-31 | 2018-12-31 21:59:48.490 | 209.8 (close) | 12-31 is not an early close |
| 2014-12-31 | 2014-12-31 21:59:47.916 | 209.8 (close) | 12-31 is not an early close |
| 2018-12-05 | 2018-12-05 21:59:25.858 | 209.4 (close) | National Day of Mourning |
| 2024-12-31 | 2024-12-31 21:58:55.257 | 208.9 (close) | 12-31 is not an early close |
| 2019-07-04 | 2019-07-04 20:56:47.058 | 206.8 (close) | Independence Day traded to 16:56 NY |
| 2022-11-25 | 2022-11-25 18:43:55.367 | −196.1 (close) | day after Thanksgiving |
| 2021-11-26 | 2021-11-26 18:43:59.260 | −196.0 (close) | day after Thanksgiving |
| 2018-11-23 | 2018-11-23 18:44:30.809 | −195.5 (close) | day after Thanksgiving |
| 2017-11-24 | 2017-11-24 18:44:51.395 | −195.1 (close) | day after Thanksgiving |
| 2016-11-25 | 2016-11-25 18:44:54.083 | −195.1 (close) | day after Thanksgiving |
| 2014-11-28 | 2014-11-28 18:44:58.682 | −195.0 (close) | day after Thanksgiving |

### `cal.closed_market_ticks` — 37 failing days, and `cal.holiday_behaviour` — 36 failing days

Both checks top out on the same two dates, and the timestamps place the offending ticks
*between the configured close and the real one*:

| Trading day | Timestamp (UTC) | New York local | Spread USD | Day metric |
| --- | --- | --- | --- | --- |
| 2025-01-20 (MLK Day) | 2025-01-20 19:09:51.447 | 14:09 | 3.027 | 2.12 % of ticks / 4,755 ticks |
| 2025-01-20 | 2025-01-20 18:39:09.460 | 13:39 | 2.160 | — |
| 2025-01-20 | 2025-01-20 19:07:38.158 | 14:07 | 1.967 | — |
| 2025-01-20 | 2025-01-20 19:13:09.814 | 14:13 | 1.940 | — |
| 2019-12-24 | 2019-12-24 18:42:19.087 | 13:42 | 2.090 | 0.93 % of ticks / 784 ticks |
| 2019-12-24 | 2019-12-24 18:42:18.217 | 13:42 | 2.070 | — |
| 2019-12-24 | 2019-12-24 18:42:18.505 | 13:42 | 2.030 | — |
| 2014-12-31 | 2014-12-31 21:56:08.070 | 16:56 | 1.128 | 14.38 % of ticks |
| 2014-12-31 | 2014-12-31 21:59:30.433 | 16:59 | 1.058 | — |

The remaining entries of both top-20 lists are further ticks on 2025-01-20 and 2019-12-24 in the
same minutes.

### `bar.extreme_returns` — 31 failing days

| Trading day | Timestamp (UTC) | Robust z of the 1-minute return | New York local | Day metric |
| --- | --- | --- | --- | --- |
| 2016-09-02 | 2016-09-02 12:30 | 121.1 | 08:30 | 11 |
| 2016-07-08 | 2016-07-08 12:30 | −78.6 | 08:30 | 14 |
| 2015-10-02 | 2015-10-02 12:30 | 66.7 | 08:30 | 12 |
| 2016-08-26 | 2016-08-26 14:00 | −51.93 | 10:00 | 13 |
| 2022-10-13 | 2022-10-13 12:30 | −47.76 | 08:30 | 11 |
| 2015-12-16 | 2015-12-16 19:00 | −47.48 | 14:00 | 21 |
| 2021-09-03 | 2021-09-03 12:30 | 47.37 | 08:30 | 18 |
| 2015-10-28 | 2015-10-28 18:00 | −47.12 | 14:00 | 13 |
| 2014-05-02 | 2014-05-02 12:30 | −46.42 | 08:30 | 12 |
| 2015-12-16 | 2015-12-16 19:03 | 45.21 | 14:03 | 21 |
| 2019-07-31 | 2019-07-31 18:00 | −44.55 | 14:00 | 21 |
| 2016-12-14 | 2016-12-14 19:00 | −43.95 | 14:00 | 11 |
| 2021-08-06 | 2021-08-06 12:30 | −43.33 | 08:30 | 13 |
| 2021-06-16 | 2021-06-16 18:00 | −41.40 | 14:00 | 12 |
| 2021-05-07 | 2021-05-07 12:30 | 39.67 | 08:30 | 18 |
| 2021-06-16 | 2021-06-16 20:42 | −39.10 | 16:42 | 12 |
| 2014-04-24 | 2014-04-24 13:45 | 38.51 | 09:45 | 12 |
| 2016-08-26 | 2016-08-26 14:09 | 37.55 | 10:09 | 13 |
| 2016-08-26 | 2016-08-26 14:10 | 37.24 | 10:10 | 13 |
| 2015-03-18 | 2015-03-18 18:00 | 34.07 | 14:00 | 18 |

### `tick.spread_outliers` — 5 failing days

Every one of the top 20 anomalies is on 2020-03-24 between 12:13:15 and 12:13:37 UTC, with the
spread at 17.25–17.48 USD against an hour-of-week p50 of 0.32 USD (ratios 53.9–54.6). The five
failing days are 2020-03-24 (47.6 % of ticks), 2020-03-25 (24.2 %), 2020-03-26 (8.2 %),
2020-04-07 (3.5 %) and 2020-03-30 (2.1 %).

### `cal.missing_open_data` — 18 failing days, and `tick.rate_anomalies` — 37 failing days

All 20 top anomalies of `cal.missing_open_data` are whole missing hours ("60 missing minutes in
this hour") on 2014-08-25 and 2014-10-28 … 2014-10-31. All 20 top anomalies of
`tick.rate_anomalies` are hours whose tick count is 0 or near 0 against their hour-of-week norm
(2019-11-29 19:00 at 0.0035 of norm, 2014-03-20 22:00 at 0.00054, the rest exactly 0), on
2020-03-26, 2020-04-13, 2020-05-06, 2020-05-28, 2020-08-18 and 2020-11-27.

## 4. Calendar (DQ-004) against `config/sessions.yaml`

### 4.1 First and last tick per weekday per year

**Observed.** Mode of the first and last tick of each trading day, in New York local time, over the
3,028 graded days:

| Year | First tick Mon–Fri | Last tick Mon–Fri |
| --- | --- | --- |
| 2014 … 2025 (every year, every weekday) | **18:00** | **16:59** |

The first tick always falls on the **previous** calendar date (offset −1 day), i.e. the trading day
opens at 18:00 New York the evening before, exactly as `market.open` and the 17:00 roll prescribe.
Of 3,028 days, 2,919 open at 18:00 and 2,675 close at 16:59; the exceptions are enumerated in §4.3
and §5.

### 4.2 Friday close and Sunday open in UTC across US DST

**Observed.** 595 weekends, split by the US DST regime in force (mode, then the range):

| Year | DST | n | Friday last tick UTC | → New York | Sunday first tick UTC | → New York |
| --- | --- | --- | --- | --- | --- | --- |
| 2014 | EDT | 32 | 20:59:58 | 16:59 | 22:00:00 | 18:00 |
| 2014 | EST | 17 | 21:59:59 | 16:59 | 23:00:02 | 18:00 |
| 2015 | EDT | 33 | 20:59:01 | 16:59 | 22:00:01 | 18:00 |
| 2015 | EST | 17 | 21:59:55 | 16:59 | 23:00:02 | 18:00 |
| 2016 | EDT | 33 | 20:59:57 | 16:59 | 22:00:00 | 18:00 |
| 2016 | EST | 16 | 21:59:53 | 16:59 | 23:00:08 | 18:00 |
| 2017 | EDT | 33 | 20:59:00 | 16:59 | 22:00:00 | 18:00 |
| 2017 | EST | 16 | 21:58:59 | 16:59 | 23:00:00 | 18:00 |
| 2018 | EDT | 33 | 20:58:00 | 16:58 | 22:00:00 | 18:00 |
| 2018 | EST | 18 | 21:59:55 | 16:59 | 23:00:00 | 18:00 |
| 2019 | EDT | 33 | 20:58:00 | 16:58 | 22:00:00 | 18:00 |
| 2019 | EST | 18 | 21:59:55 | 16:59 | 23:00:00 | 18:00 |
| 2020 | EDT | 33 | 20:59:58 | 16:59 | 22:00:00 | 18:00 |
| 2020 | EST | 17 | 21:59:56 | 16:59 | 23:00:00 | 18:00 |
| 2021 | EDT | 33 | 20:59:58 | 16:59 | 22:00:00 | 18:00 |
| 2021 | EST | 17 | 21:59:58 | 16:59 | 23:00:00 | 18:00 |
| 2022 | EDT | 33 | 20:59:58 | 16:59 | 22:00:00 | 18:00 |
| 2022 | EST | 16 | 21:59:58 | 16:59 | 23:00:00 | 18:00 |
| 2023 | EDT | 33 | 20:59:58 | 16:59 | 22:00:00 | 18:00 |
| 2023 | EST | 16 | 21:59:58 | 16:59 | 23:00:00 | 18:00 |
| 2024 | EDT | 33 | 20:59:58 | 16:59 | 22:00:01 | 18:00 |
| 2024 | EST | 18 | 21:59:58 | 16:59 | 23:00:01 | 18:00 |
| 2025 | EDT | 33 | 20:59:59 | 16:59 | 22:00:01 | 18:00 |
| 2025 | EST | 14 | 21:59:58 | 16:59 | 23:00:00 | 18:00 |

**Evidence and a correction.** The weekly close is 16:59 New York and the weekly open 18:00 New
York in **both** DST regimes, in every year; in UTC the boundary moves with US DST exactly as the
configuration's per-date conversion produces. In particular, on 2024-03-01 the last tick was at
**21:59:59.793 UTC**, verified directly in the raw CSV (`1709330399793`) — 16:59:59 New York EST,
one second before the calendar's 17:00 close.

The observation carried in ADR 0062, `docs/STATUS.md` and `docs/runbooks/real-data.md` — that the
last Friday tick on 2024-03-01 was at 20:59:59 UTC, an hour before the calendar's close — is
**wrong**, and it was the only evidence suggesting the weekly close needed changing. It does not.

**Interpretation.** `market.tz: America/New_York`, `open: "18:00"`, `close: "17:00"`,
`week_open: "18:00"` and `trading_weekdays: [mon … fri]` are confirmed on twelve years of real
Dukascopy ticks. The `dukascopy` source's declared clock (`UTC`) is confirmed too: a misdeclared
clock would move every boundary by a whole hour and `cal.gap_location` would fail on essentially
every day, not on 191.

**Limitations.** The boundary is observed from the vendor's ticks, which stop one second before the
close and resume on the second at the open; this review cannot distinguish "the venue closed" from
"the vendor stopped recording".

**Action.** No change to the weekly or daily boundary. Correct the recorded observation (§7.4).

### 4.3 The daily break

**Observed.** The New York hour 17:00–17:59 contains **zero** ticks in every one of the twelve
years (the hour is absent from the per-hour-of-week profile of §5.1 in all years). The last tick
before it is at 16:59 and the first after it at 18:00.

**Interpretation.** The 60-minute daily break at 17:00–18:00 New York is exactly as configured.

**Action.** None.

### 4.4 Holidays and early closes

**Observed.** 124 dates in the graded window are a US-calendar holiday or match the
`early_close_dates` rule.

| Holiday (config `us_calendar: NYSE`) | Config expects | n | Traded | Observed last tick, New York |
| --- | --- | --- | --- | --- |
| New Year's Day | closed | 11 | 0 | — |
| Good Friday | closed | 12 | 0 | — |
| Christmas Day | closed | 11 | 0 | — |
| Martin Luther King Jr. Day | early close 13:30 | 12 | 12 | 11:59 … 14:29 |
| Washington's Birthday | early close 13:30 | 12 | 12 | 12:29 … 14:29 |
| Memorial Day | early close 13:30 | 12 | 12 | 12:59 / 13:00 / 14:28 |
| Juneteenth | early close 13:30 | 4 | 4 | 14:28 / 14:29 |
| Independence Day | early close 13:30 | 12 | 12 | 12:58 … 16:56 |
| Labor Day | early close 13:30 | 12 | 12 | 12:59 … 14:28 |
| Thanksgiving Day | early close 13:30 | 11 | 11 | 12:58 … 14:28 |
| National Day of Mourning (G. H. W. Bush, 2018-12-05) | early close 13:30 | 1 | 1 | **16:59** |
| National Day of Mourning (J. Carter, 2025-01-09) | early close 13:30 | 1 | 1 | **16:59** |
| `early_close_dates` 12-24 / 12-31 | early close 13:30 | 13 | 13 | 13:30 … 16:59 |

**Evidence.** Three distinct errors, each visible on its own:

1. **The early-close time moved from 13:00 to 14:30 New York in 2022.** Last tick by year on the
   holiday early closes: 2014–2021 cluster at 12:58–13:00; 2022–2025 cluster at 14:28–14:29.
   Before 2022 the configured 13:30 close is **30 minutes too late** (it marks 13:00–13:30 as open
   when it is not, which `cal.missing_open_data` and `cal.gap_location` see); from 2022 it is
   **60 minutes too early** (ticks from 13:30 to 14:29 are flagged `CLOSED_MARKET`, which
   `cal.closed_market_ticks` and `cal.holiday_behaviour` see — hence 6, 5, 9 and 6 failures in
   2022, 2023, 2024 and 2025). The worst case is 2025-01-20 (MLK Day): 4,755 ticks between 13:30
   and 14:29 New York, 2.12 % of the day.
2. **12-31 is not an early close.** On every one of the seven occurrences in the window
   (2014, 2015, 2018, 2019, 2020, 2021, 2024) gold traded to 16:58–16:59 New York, a full day.
   12-24 *is* an early close, but at **13:43–13:45**, not 13:30 (2015 is the one occurrence at
   13:30). The configured 13:30 close on 12-31 produces `cal.gap_location` ≈ 210 minutes and the
   worst `cal.closed_market_ticks` metric in the report (2014-12-31, 14.38 % of the day's ticks).
3. **The day after Thanksgiving is an early close and is not configured as one.** Every year
   2014–2024 it ends at 12:43–14:43 New York (mode 13:44) while the configuration treats it as a
   normal day closing at 17:00. It is the largest `cal.gap_location` failure in the report
   (2023-11-24, 256 minutes) and appears in the top of `bar.missing_minutes` and
   `tick.stale_quotes` for every year.
4. **The National Days of Mourning are not gold holidays.** NYSE closed on 2018-12-05 and
   2025-01-09; Dukascopy XAUUSD traded a full day (56,644 and 208,268 ticks, last tick 16:59).
   The `holidays` NYSE financial calendar is the wrong authority for these two dates.

Irregular cases that fit none of the rules: 2019-07-04 traded to 16:56 New York (a near-full day on
Independence Day) and 2019-09-02 (Labor Day) to 14:17; 2024-07-04 to 13:59; MLK Day 2014 ended at
11:59 and 2017 at 12:30.

**Interpretation.** The full-close list (New Year's Day, Good Friday, Christmas Day) is exactly
right — zero ticks on all 34 occurrences. Everything about the early closes is wrong in at least
one direction, and the errors are a venue-calendar problem, not a data problem: the ticks are
there, the configuration misclassifies them. Because `one schedule for every year` is assumed
(ADR 0002), the 2022 change cannot be expressed at all today.

**Limitations.** 124 dates, so between 1 and 12 observations per holiday per regime; Juneteenth has
only 4. The irregular 2019 dates are unexplained by any rule this review can see, and no venue
calendar was consulted (the Dukascopy hours pages are unreachable, ADR 0057, and no execution venue
is chosen).

**Action.** Proposed calendar changes in §7.1. Nothing changed.

## 5. Export holes, spreads and tick density

### 5.1 Spread profile by New York hour of week, per year

Median spread in USD, averaged over Monday–Friday, by New York hour of day. The 17:00 hour is
absent in every year (§4.3).

| Year | 00 | 03 | 06 | 08 | 10 | 12 | 14 | 16 | 18 | 19 | 21 | 23 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2014 | 0.293 | 0.282 | 0.280 | 0.286 | 0.279 | 0.280 | 0.285 | 0.351 | **0.360** | 0.333 | 0.266 | 0.284 |
| 2015 | 0.313 | 0.300 | 0.300 | 0.300 | 0.289 | 0.294 | 0.323 | 0.369 | **0.408** | 0.349 | 0.322 | 0.314 |
| 2016 | 0.314 | 0.298 | 0.291 | 0.286 | 0.283 | 0.290 | 0.308 | 0.341 | **0.398** | 0.352 | 0.320 | 0.312 |
| 2017 | 0.245 | 0.231 | 0.233 | 0.231 | 0.230 | 0.235 | 0.244 | 0.260 | **0.296** | 0.268 | 0.243 | 0.244 |
| 2018 | 0.238 | 0.222 | 0.223 | 0.228 | 0.227 | 0.230 | 0.240 | 0.254 | **0.268** | 0.250 | 0.234 | 0.236 |
| 2019 | 0.296 | 0.287 | 0.287 | 0.292 | 0.289 | 0.288 | 0.306 | 0.330 | **0.356** | 0.314 | 0.305 | 0.297 |
| 2020 | 0.386 | 0.397 | 0.386 | 0.400 | 0.394 | 0.385 | 0.389 | 0.458 | **0.520** | 0.410 | 0.417 | 0.408 |
| 2021 | 0.369 | 0.352 | 0.345 | 0.355 | 0.349 | 0.343 | 0.345 | 0.395 | **0.407** | 0.388 | 0.364 | 0.359 |
| 2022 | 0.388 | 0.362 | 0.345 | 0.352 | 0.343 | 0.343 | 0.357 | 0.396 | **0.439** | 0.400 | 0.397 | 0.389 |
| 2023 | 0.338 | 0.328 | 0.329 | 0.333 | 0.329 | 0.329 | 0.330 | 0.372 | **0.399** | 0.344 | 0.338 | 0.338 |
| 2024 | 0.406 | 0.379 | 0.381 | 0.383 | 0.382 | 0.380 | 0.370 | 0.357 | **0.436** | 0.412 | 0.405 | 0.405 |
| 2025 | 0.588 | 0.554 | 0.557 | 0.568 | 0.576 | 0.555 | 0.552 | 0.554 | **0.672** | 0.628 | 0.620 | 0.602 |

**Observed.** The widest hour of the day is the 18:00 reopen in every one of the twelve years, with
16:00 (the pre-close) second. The reopen median runs 1.15× to 1.40× the midday median; at the 90th
percentile the ratio reaches 2.6× (2020: 1.554 vs 0.605 at hour 10).

**Evidence for ADR 0013 item 2 (hourly vs 15-minute buckets).** Spread by 15-minute New York
bucket around the break:

| Year | 16:00 | 16:15 | 16:30 | 16:45 | 18:00 | 18:15 | 18:30 | 18:45 | hour 18 p50 | worst 15 min inside hour 18 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2014 | 0.327 | 0.331 | 0.337 | 0.378 | 0.442 | 0.359 | 0.359 | 0.359 | 0.363 | ×1.22 |
| 2016 | 0.318 | 0.322 | 0.359 | 0.418 | 0.460 | 0.402 | 0.389 | 0.372 | 0.401 | ×1.15 |
| 2018 | 0.248 | 0.250 | 0.253 | 0.271 | 0.304 | 0.266 | 0.260 | 0.259 | 0.269 | ×1.13 |
| 2020 | 0.408 | 0.436 | 0.467 | 0.554 | 0.653 | 0.527 | 0.486 | 0.456 | 0.537 | ×1.22 |
| 2022 | 0.379 | 0.384 | 0.397 | 0.427 | 0.514 | 0.459 | 0.433 | 0.419 | 0.453 | ×1.13 |
| 2023 | 0.339 | 0.336 | 0.423 | 0.450 | 0.497 | 0.430 | 0.379 | 0.360 | 0.417 | ×1.19 |
| 2025 | 0.539 | 0.539 | 0.556 | 0.590 | 0.849 | 0.677 | 0.666 | 0.650 | 0.699 | ×1.21 |

Across all twelve years the hourly bucket understates the worst 15 minutes inside it by ×1.06 to
×1.23 for the 16:00 hour and ×1.07 to ×1.22 for the 18:00 hour.

**Interpretation.** The rollover spike is real, is concentrated in 16:45–18:15 New York (which is
exactly the configured `rollover` event window, ADR 0026), and an hourly bucket hides at most about
a quarter of it on the median. A 23 % understatement is immaterial to a check that fires at **10×**
the bucket median, and the `SPREAD_OUTLIER` cleaning flag fired on only 1,079 of 504 million ticks
(0.0002 %) — so the known issue "the `SPREAD_OUTLIER` flag fires on rollover widening" does not
show up in the data at all. It matters more for cost modelling, where the rollover multiplier is
already a separate, explicit mechanism.

**Limitations.** These are vendor spreads from a venue that is not an execution venue; they may
never be used as execution costs (ADR 0057). Quantiles are read off 0.001 USD histograms, so they
are exact to one tenth of a cent.

**Action.** Proposal in §7.2 item 5: keep hourly buckets.

### 5.2 Tick density per year

| Year | Days | Ticks | Ticks per day | `SPIKE` share | Export holes | Market minutes lost | Share of the year's market minutes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2014 | 258 | 20,592,678 | 79,817 | 0.043 % | 205 | 13,380 | 3.73 % |
| 2015 | 258 | 25,672,492 | 99,506 | 0.182 % | 16 | 960 | 0.27 % |
| 2016 | 258 | 46,200,033 | 179,070 | 0.181 % | 0 | 0 | 0 % |
| 2017 | 257 | 45,738,979 | 177,973 | 0.222 % | 6 | 360 | 0.10 % |
| 2018 | 258 | 35,191,107 | 136,400 | 0.490 % | 108 | 6,720 | 1.87 % |
| 2019 | 258 | 35,856,822 | 138,980 | 0.313 % | 137 | 8,400 | 2.34 % |
| 2020 | 259 | 53,283,678 | 205,728 | 0.129 % | 235 | 15,120 | 4.21 % |
| 2021 | 258 | 51,158,077 | 198,287 | 0.107 % | 216 | 13,560 | 3.78 % |
| 2022 | 258 | 53,559,687 | 207,596 | 0.096 % | 31 | 1,860 | 0.52 % |
| 2023 | 257 | 36,017,402 | 140,146 | 0.059 % | 62 | 3,780 | 1.05 % |
| 2024 | 259 | 54,451,610 | 210,238 | 0.142 % | 176 | 10,860 | 3.03 % |
| 2025 (to 09-25) | 190 | 46,549,879 | 244,999 | 0.091 % | 11 | 720 | 0.20 % |

**Observed.** Density rises from ~80k ticks a day in 2014 to ~245k in 2025, with a dip in 2018,
2019 and 2023.

**Interpretation.** Twelve years of this feed are not one homogeneous sample. Any check whose
threshold is an absolute per-day count (`tick.spikes`, `bar.extreme_returns`) will behave
differently in 2014 and in 2025 for reasons that have nothing to do with data quality.

### 5.3 Export holes: whole missing hours

**Observed.** 1,203 gaps that fall inside configured market hours begin **and** end within 5
seconds of a whole UTC hour and last 1 hour (1,051), 2 hours (149) or 3 hours (3). Together they
account for **75,720 market minutes, 1.81 % of the pre-vault market minutes**, and for 86.4 % of
all market minutes inside any gap over 10 minutes.

**Evidence.** The hour is dukascopy-node's fetch unit, and the download used `-r 3 -re -fr`, which
the runbook records as "an hour skipped after its retries is missing from the CSV". Gold does not
fall silent for exactly 60.00 minutes, 1,051 times, always between whole UTC hours. 2016 has zero
such holes, which is what an undamaged export of this feed looks like. The affected months match
those flagged in §1.

**Interpretation.** The holes are a download artefact, not a market or vendor-data property. They
are the single cause behind the bulk of four checks' failures (§6).

**Limitations.** A hole and a genuine venue outage of exactly one hour are indistinguishable from
the CSV alone; the hour-boundary alignment and the `-fr` flag are what make the artefact reading
compelling, not proof. Re-exporting an affected month would settle it; nothing was fetched in this
session.

**Action.** §7.3 (re-export) and §7.5 (exclusions). No data was repaired — raw data is immutable
and cleaning only flags.

## 6. Triage of the failing checks

| Check | Fails | Real market event | Feed artefact (export holes) | Rule unsuited to this feed | Wrong calendar |
| --- | --- | --- | --- | --- | --- |
| `tick.spikes` | 1,652 | the tail (2020-03, 2025-04) | — | **the bulk** | — |
| `tick.stale_quotes` | 651 | 0 | **597** | double-counts §5.3 | 54 |
| `bar.missing_minutes` | 627 | 0 | **597** | — | 30 |
| `cal.gap_location` | 191 | 0 | **140** | — | **51** |
| `cal.closed_market_ticks` | 37 | 0 | 0 | — | **37** |
| `cal.holiday_behaviour` | 36 | 0 | 0 | — | **36** |
| `tick.rate_anomalies` | 37 | 0 | **31** | — | 6 |
| `bar.extreme_returns` | 31 | **31** | 0 | — | 0 |
| `cal.missing_open_data` | 18 | 0 | **15** | — | 3 |
| `tick.spread_outliers` | 5 | **5** | 0 | — | 0 |

### 6.1 `tick.spikes` — the rule, not the data

**Observed.** 1,652 of 3,028 days fail (55 %) and only 131 pass. In 2018, 254 of 258 days fail and
none passes. Spike events per day, median by year: 9 (2014), 55 (2015), 100 (2016), 139 (2017),
**228** (2018), 123 (2019), 54 (2020), 55 (2021), 40 (2022), 22 (2023), 93 (2024), 54 (2025),
against a warn level of 5 and a fail level of 50.

**Evidence.** The magnitude of the flagged jumps, from the recorded anomalies (the five largest per
day):

| Year | Median \|mid jump\| USD | In bp of price | 90th pct USD | Max USD |
| --- | --- | --- | --- | --- | --- |
| 2014 | 0.200 | ~1.6 | 0.453 | 5.92 |
| 2018 | 0.135 | ~1.0 | 0.244 | 3.82 |
| 2020 | 0.285 | ~1.6 | 0.870 | 14.12 |
| 2024 | 0.310 | ~1.3 | 0.547 | 4.29 |
| 2025 | 0.465 | ~1.4 | 0.911 | 7.48 |

The typical flagged "spike" is a **~1 bp mid move that reverts within 5 ticks**, and 841,729 ticks
in total (0.167 %) carry the flag. The rule's robust-scale floor, `min_scale_bps: 0.05`, means that
in a quiet window 8 robust sigma is only 0.4 bp — far below any economically meaningful
mis-quote — so in quiet periods the floor, not the 8-sigma test, sets the bar.

**Interpretation.** At the extreme tail the check is right: the largest anomalies are 6–14 USD
reverting jumps on 2020-03-24/25 and 2025-04-07, which are genuine disorderly-market prints. In the
bulk it is measuring bid/ask bounce in a high-rate aggregated tick feed. A check that fires on 55 %
of all days and 95 % of a given year carries no information and, through DQ-007, would block more
than half the history. The threshold (plan default: 5 warn, 50 fail "reverting spikes above 8 robust
σ per day") was written for a broker feed at a far lower tick rate; §5.2 shows the rate rises
threefold over the window, so an absolute per-day count cannot be regime-neutral either.

Note that `SPIKE` is deliberately **not** in `bars.exclude_flags` (it is confirmed by later ticks
and so non-causal, ADR 0007), so these flags never affect bar prices; the consequence is confined to
the quality grade and the DQ-007 gate.

**Limitations.** "Economically meaningful" is a judgement, not a measurement. No returns analysis
was run and no strategy result exists, so the proposal below is argued only from the magnitude
distribution and the share of days failing.

**Action.** §7.2 items 1 and 2. Nothing changed.

### 6.2 `tick.stale_quotes` — a second detector of the same artefact

**Observed.** 651 fails, and every one of them is accounted for: on **625** the longest single
"quote unchanged" run is at least 55 minutes (574 are exactly 60 minutes and 34 exactly 120), and
**54** fall on a holiday or early-close day where the calendar expects hours of trading that never
happens. The two sets overlap by 28, leaving **597 export holes, 54 calendar, 0 unexplained**. No
failing day has a longest run under 10 minutes. 559 of the 651 have an export hole inside an active
session, and export-hole seconds explain 82 % of the stale seconds counted on failing days.

**Evidence.** The `STALE` cleaning flag — which marks ticks following a genuinely unchanged quote —
fires on **9 ticks in 504 million**. The metric counts time between quote *changes*, so a period
with no ticks at all scores identically to a frozen feed.

**Interpretation.** On this feed `tick.stale_quotes` finds no frozen quotes. It re-detects the
missing hours that `bar.missing_minutes` and `cal.missing_open_data` already report, and the three
checks fail together on the same days, tripling the apparent severity of one defect. The check is
not wrong — a silent feed is a real defect — but it is redundant here and its 1,800 s fail level
is crossed by a single missing hour.

**Limitations.** Separating silence from a frozen quote requires a rule change, not a threshold
change, so this review can only describe the conflation.

**Action.** §7.2 item 3. Nothing changed.

### 6.3 `bar.extreme_returns` and `tick.spread_outliers` — real market events

**Observed.** Of 4,984 recorded extreme-return anomalies the most common New York minute is
**08:30** (363), then 10:00 (163), 08:31 (90), 08:32 (81), 08:20 (74) and 14:00 (70). 12 % land
exactly on 08:30, 10:00 or 14:00 — the US data release, the 10:00 release slot and the FOMC
statement — and 7.3 % on 08:30 alone. The 08:20 cluster is the COMEX open. The largest single
value, z = 121 at 2016-09-02 12:30 UTC, is the August 2016 payrolls minute; z = −47.5 at
2015-12-16 19:00 UTC is the first Fed hike.

All five `tick.spread_outliers` failures are 2020-03-24 … 2020-04-07, with spreads of 17.25–17.48
USD against an hour-of-week p50 of 0.32 USD on 2020-03-24 — the COVID gold dislocation. The warn
days include 2021-08-09 (the Sunday-night gold flash crash) and 2025-04-02.

**Interpretation.** Both checks are correctly calibrated: they are quiet for twelve years and fire
on the days a gold researcher would name from memory. 639 extreme-return *warns* against a warn
level of 2 is also as designed — ADR 0011 set that level expecting "a few legitimately extreme
minutes" on news days.

**Limitations.** Release minutes were identified from the clock, not from an economic calendar; no
event study was run and none is permitted before the DQ-008 sign-off.

**Action.** None. These two checks need no change.

## 7. Proposals — nothing applied

All of the following require the owner's approval. ADR 0013 item 1 allows the quality thresholds to
change **once**, through an ADR, after this review; the draft is
`docs/adr/0069-dq-008-quality-thresholds.md`, **status "proposed"**. No threshold, calendar value or
exclusion was changed in this session, and none of these proposals derives from a strategy result.

### 7.1 Calendar (`config/sessions.yaml`)

| # | Proposal | Evidence |
| --- | --- | --- |
| C1 | Keep `market.tz`, `open: "18:00"`, `close: "17:00"`, `week_open: "18:00"`, `trading_weekdays` **unchanged**. | §4.1, §4.2: 3,028 days and 595 weekends agree exactly, in both DST regimes |
| C2 | Keep the full-close list (New Year's Day, Good Friday, Christmas Day) **unchanged**. | §4.4: zero ticks on all 34 occurrences |
| C3 | Make the holiday early-close time **date-dependent**: 13:00 New York up to and including 2021, 14:30 from 2022. This needs a schema change — ADR 0002's "one schedule for every year" no longer holds. | §4.4 item 1: last tick clusters at 12:58–13:00 for 2014–2021 and 14:28–14:29 for 2022–2025 |
| C4 | Remove `12-31` from `early_close_dates`; it is a full trading day. | §4.4 item 2: seven occurrences, all to 16:58–16:59 New York |
| C5 | Keep `12-24` as an early close but at **13:45** New York, not 13:30. | §4.4 item 2: 13:43–13:45 on six of seven occurrences |
| C6 | Add the **day after Thanksgiving** as an early close at 13:45 New York. | §4.4 item 3: 12:43–14:43 (mode 13:44) every year 2014–2024; the largest `cal.gap_location` failure in the report |
| C7 | Exclude the **National Days of Mourning** (2018-12-05, 2025-01-09) from the holiday rules — gold traded a full day. Either name them as exceptions or stop treating every NYSE holiday as a gold holiday. | §4.4 item 4: 56,644 and 208,268 ticks, last tick 16:59 |
| C8 | Leave the irregular dates (2019-07-04 to 16:56, 2019-09-02 to 14:17, 2024-07-04 to 13:59, MLK 2014 to 11:59, MLK 2017 to 12:30) **unmodelled**, and let the calendar checks keep flagging them. | §4.4: no rule explains them, and 1–2 observations cannot support one |

C3 and C6 together account for 51 of the 191 `cal.gap_location` failures, all 37
`cal.closed_market_ticks` failures and all 36 `cal.holiday_behaviour` failures. **A changed calendar
means a new feature-set version** (ADR 0067), and it changes which minutes `cal.*` and `bar.*`
expect, so a re-validation follows any of these.

### 7.2 Thresholds (`config/quality.yaml`) — the one allowed change

Drafted in ADR 0069, status "proposed". Summary:

| # | Check | Now | Proposed | Why |
| --- | --- | --- | --- | --- |
| T1 | `tick.spikes` | warn 5, fail 50 events/day | metric in **events per million usable ticks**, warn **2000**, fail **4000** — which grades the twelve years at 2,893 pass / 132 warn / **3 fail** (0.1 %) instead of 131 / 1,245 / 1,652 | §6.1: 55 % of days fail now; the tick rate triples over the window, so an absolute daily count is not comparable across years |
| T2 | `cleaning.spike.min_scale_bps` | 0.05 bp (so the smallest flaggable spike is 0.4 bp) | **0.125 bp**, making the floor 1 bp with `z_threshold` unchanged | §6.1: the median flagged jump is ~1 bp and the floor, not the 8-sigma test, binds in quiet windows. This is a **cleaning-rule** parameter: changing it creates a new clean rules version and requires re-running `clean` and `build-bars` (~21 min), so its effect on the flag count is **not measured** |
| T3 | `tick.stale_quotes` | warn any, fail 1,800 s | **recommended:** exclude intervals with **no ticks at all** from the metric, thresholds unchanged (a rule change, making the check a genuine frozen-feed detector); **alternative:** keep the metric and demote the check to `minor` | §6.2: 9 stale ticks in 504 M; 625 of 651 failures are ≥ 55-minute silences already reported by two other checks |
| T4 | `bar.missing_minutes`, `cal.missing_open_data`, `cal.gap_location`, `tick.rate_anomalies` | as configured | **keep unchanged** | §5.3, §6: they correctly report a real defect; the fix is a better export (§7.3) and exclusions (§7.5), not a looser threshold |
| T5 | Spread buckets (ADR 0013 item 2) | New York hour of week | **keep hourly** | §5.1: an hourly bucket hides at most ×1.23 of the rollover widening on the median, against a 10× outlier test; the `SPREAD_OUTLIER` flag fires on 0.0002 % of ticks |
| T6 | `bar.extreme_returns`, `tick.spread_outliers`, and the eight checks that never fire | as configured | **keep unchanged** | §6.3, §2 |

T1, T2 and T3 are the only substantive changes proposed, and T2 is a cleaning parameter rather than
a threshold — if it is accepted, the clean store and bars are rebuilt under a new rules version and
the quality run is repeated, so the "one allowed change" should cover T1, T2 and T3 together in a
single ADR.

### 7.3 Re-export of the damaged months

Not fetched in this session (the owner's instruction). Candidates, worst first, by market minutes
lost to whole-hour holes: **2014-10** (205 holes), 2020, 2021, 2024, 2019, 2018, 2023, 2022, 2015,
2017, 2025. 2016 is clean. A re-export is a no-op by SHA-256 only if the bytes are identical, so a
repaired month arrives as a second raw file; the clean store would then have to be rebuilt for the
affected days. Worth doing for 2014-10 at least, where 3.73 % of the year's market minutes are
missing and the worst ten days of the whole window sit.

### 7.4 Documentation corrections (made in this session)

The 2024-03-01 observation recorded in ADR 0062, `docs/STATUS.md` and
`docs/runbooks/real-data.md` is wrong (§4.2). It is corrected in all three places and a correction
note is appended to ADR 0062; the calendar it questioned is confirmed, not changed.

### 7.5 Trading days proposed for exclusion

Criterion: more than 20 % of the day's **calendar market-hours** minutes missing, i.e. a
`cal.missing_open_data` FAIL, after removing the days where the calendar — not the data — is at
fault. 15 days of 3,028 (0.50 %):

| Trading day | Day | Market minutes missing | Session minutes missing | Longest missing runs |
| --- | --- | --- | --- | --- |
| 2014-08-25 | Mon | 26.1 % | 7.1 % | 11:00 UTC +60 min |
| 2014-10-13 | Mon | 43.5 % | 57.1 % | 17:00 UTC +240 min, 07:00 +60, 09:00 +60 |
| 2014-10-14 | Tue | 34.8 % | 35.7 % | 11:00 UTC +120 min, 08:00 +60, 16:00 +60 |
| 2014-10-15 | Wed | 21.7 % | 28.6 % | 15:00 UTC +120 min, 10:00 +60, 19:00 +60 |
| 2014-10-16 | Thu | 30.4 % | 21.4 % | 08:00 UTC +60 min, 15:00 +60, 20:00 +60 |
| 2014-10-20 | Mon | 39.2 % | 50.0 % | 17:00 UTC +240 min, 10:00 +120, 07:00 +60 |
| 2014-10-21 | Tue | 21.7 % | 7.1 % | 20:00 UTC +60 min |
| 2014-10-23 | Thu | 21.7 % | 28.6 % | 09:00 UTC +60 min, 12:00 +60, 14:00 +60 |
| 2014-10-24 | Fri | 21.7 % | 14.3 % | 17:00 UTC +60 min, 20:00 +60 |
| 2014-10-28 | Tue | 34.8 % | 46.2 % | 08:00 UTC +120 min, 14:00 +120, 17:00 +60 |
| 2014-10-29 | Wed | 21.7 % | 30.8 % | 09:00 UTC +120 min, 13:00 +60, 16:00 +60 |
| 2014-10-30 | Thu | 21.8 % | 23.1 % | 09:00 UTC +60 min, 11:00 +60, 16:00 +60 |
| 2014-10-31 | Fri | 34.8 % | 46.2 % | 12:00 UTC +120 min, 18:00 +120, 09:00 +60 |
| 2020-12-11 | Fri | 21.7 % | 7.1 % | 09:00 UTC +60 min |
| 2023-12-28 | Thu | 21.7 % | 21.4 % | 09:00 UTC +120 min, 14:00 +60 |

Eleven of the fifteen are the consecutive run **2014-10-13 … 2014-10-31**, which is better treated
as one damaged stretch than as eleven independent days.

**Not** proposed for exclusion, although they fail `cal.missing_open_data`: 2019-11-29, 2020-11-27,
2021-11-26 — these are days after Thanksgiving, where the minutes are "missing" only because the
calendar expects trading that never happens (C6 fixes them).

**Also not** proposed for exclusion: the 597 days failing `bar.missing_minutes` from a single
missing hour. One hour of 23 is 4.3 % of the trading day, above the 5 %-of-session-minutes fail
level but not enough to discard a day; DQ-007 lists them as warnings in the dataset manifest, which
is the right treatment. If the owner prefers a stricter rule, a defensible alternative is to exclude
any day with **two or more** missing hours.

## 8. Overall interpretation and limitations

**Interpretation.** The Dukascopy CSV feed is structurally sound and the data layer's invariants
hold on 504 million real ticks: no clock reversal, no DST artefact, no duplicate, no crossed quote,
no OHLC or basis inconsistency, no duplicate bar start, zero rows dropped. The calendar's trading
boundaries — the 18:00 open, the 17:00 close and roll, the 60-minute break, the Sunday open, the
three full-close holidays — are confirmed exactly; the earlier suggestion that the weekly close was
an hour out was a mis-reading. What the report actually found is three things: the holiday
early-close rules are wrong in several ways and changed in 2022; about 1.8 % of market minutes are
missing as whole hours because of the download flags; and two checks (`tick.spikes`, and
`tick.stale_quotes` on this feed) are calibrated for a different kind of feed and currently fail on
most days.

**Limitations of this review.**

- Pre-vault only. Nothing after 2025-09-25 was read, so the vault period's quality is unknown and
  will be graded inside the GATE-002 procedure (ADR 0013 item 4).
- One source, one vendor. DQ-005 (feed consistency) is not built, so no second feed cross-checks a
  single Dukascopy quote. A systematically wrong vendor price would pass every check here.
- No venue calendar was consulted: the Dukascopy hours pages are unreachable and no execution venue
  is chosen (ADR 0057). The calendar proposals are inferred from the ticks alone.
- Export holes and genuine one-hour venue outages are indistinguishable from the CSVs; the
  artefact reading rests on hour-boundary alignment and the `-fr` download flag.
- `tick.rate_anomalies` needs four weeks of history per hour of week, so early 2014 is graded
  against a thin norm; `tick.duplicates_*` is uninformative because only one Dukascopy format was
  ingested.
- The judgement that a ~1 bp reverting tick move is not a data defect is a judgement. No returns
  analysis, EDA, dataset build or model run was performed, and no strategy result exists.
- Spreads are vendor spreads from a non-execution venue and may never be used as costs.

**Action and what this unblocks.** Sprint 2 stays "implemented and tested, not validated on real
data" until the owner rules on §7. Once the calendar (§7.1) and the threshold ADR (§7.2) are
decided and a quality run has been repeated under them, DQ-008 is closed and Phase 2 can be marked
validated on real data; ADR 0013 item 5 is then met.

---

## 9. Phase 1 of the rebuild under the accepted rules (C-36, 2026-10-06)

The owner's decisions on §7 were taken in C-35 and implemented: ADR 0069 (thresholds, the one
change ADR 0013 item 1 allowed, now spent), ADR 0070 (the calendar, version **s2**) and ADR 0071
(re-export supersession and the exclusion list). This section records the rebuild and re-grading
those decisions require. **Phase 2 — the re-export, the exclusion list and the closing verdict —
is not done yet**; this section is Phase 1 only.

### 9.1 Observed — the rebuild

| Step | Runtime | Result |
| --- | --- | --- |
| `xq clean` | 18 m 41 s | clean rules **`c2-5b9e432f`** (spike floor 0.125 bp, calendar s2): 3,075 trading days, 520,973,737 ticks, **195,979 flagged** (c1: 1,030,837), **0 dropped** |
| `xq build-bars` | 3 m 49 s | build **`b1-8bac6104`**: 144 months; 1m 4,134,802 · 5m 827,430 · 15m 275,847 · 30m 137,953 · 1h 69,006 · 4h 18,410 · 1d 3,075 — identical counts to `b1-95614b4b` |
| `xq spread-stats` | 1 m 15 s | 115 hours of week from 504,272,444 pre-vault ticks, cut at `vault.start` |
| `xq validate` | 2 m 44 s | quality run **`01M48QC50R94T5RX5FAW3D4DMT`**, 3,028 pre-vault trading days: **48,741 pass, 1,974 warn, 855 fail** (was 44,940 / 3,341 / 3,285) |

`--include-vault` was never used and no analysis in this section read a trading day after
2025-09-25. Before the old store was removed, the new one was checked day for day and tick for
tick against it: both hold 3,075 partitions, the same 2014-01-02 … 2025-12-01 span and the same
520,973,737 ticks, and no `c1` day is absent from `c2`. The bar row counts are identical because
neither `SPIKE` nor `CLOSED_MARKET` is in `bars.exclude_flags` — the two changes alter flags and
grades, not bar prices. `xq verify-raw` passes. The retired `c1-9215d40e` clean store and the
`b1-95614b4b` bars were then deleted (7.34 GB of derived data, rebuildable from `data/raw`) and
logged in `data/removed_stores.log`; their manifest rows are kept as the record of the run that
produced `01M47ZDA2E631VVD7703MXWMQN`.

### 9.2 Evidence — pass / warn / fail per check per year, old run against new

Cells are `pass/warn/fail`. `old c1/s1` is run `01M47ZDA2E631VVD7703MXWMQN`; `new c2/s2` is
`01M48QC50R94T5RX5FAW3D4DMT`. Both graded the same 3,028 trading days with the same 18 checks.

| Check | Run | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bar.basis_consistency` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `bar.duplicate_starts` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `bar.extreme_returns` | old c1/s1 | 151/103/4 | 149/103/6 | 194/60/4 | 199/56/2 | 225/33/0 | 194/60/4 | 226/32/1 | 206/47/5 | 211/44/3 | 207/49/1 | 217/41/1 | 179/11/0 |
|  | **new c2/s2** | 151/103/4 | 149/103/6 | 194/60/4 | 199/56/2 | 225/33/0 | 194/60/4 | 226/32/1 | 206/47/5 | 211/44/3 | 207/49/1 | 217/41/1 | 179/11/0 |
| `bar.missing_minutes` | old c1/s1 | 160/6/92 | 240/4/14 | 251/6/1 | 245/4/8 | 184/6/68 | 175/6/77 | 135/5/119 | 147/6/105 | 240/1/17 | 218/3/36 | 176/0/83 | 181/2/7 |
|  | **new c2/s2** | 164/2/92 | 244/2/12 | 258/0/0 | 250/2/5 | 189/1/68 | 180/3/75 | 140/1/118 | 154/0/104 | 241/0/17 | 218/1/38 | 177/0/82 | 181/1/8 |
| `bar.ohlc_consistency` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `bar.zero_range` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `cal.closed_market_ticks` | old c1/s1 | 256/0/2 | 256/1/1 | 258/0/0 | 257/0/0 | 255/0/3 | 254/2/2 | 257/0/2 | 257/0/1 | 252/0/6 | 252/0/5 | 250/0/9 | 184/0/6 |
|  | **new c2/s2** | 254/4/0 | 252/6/0 | 253/5/0 | 255/2/0 | 256/2/0 | 254/4/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/1 | 190/0/0 |
| `cal.gap_location` | old c1/s1 | 210/6/42 | 247/5/6 | 251/6/1 | 248/5/4 | 232/6/20 | 235/4/19 | 230/5/24 | 233/6/19 | 249/1/8 | 244/2/11 | 226/2/31 | 183/1/6 |
|  | **new c2/s2** | 218/0/40 | 253/2/3 | 258/0/0 | 253/3/1 | 240/1/17 | 240/1/17 | 237/0/22 | 240/1/17 | 256/0/2 | 249/0/8 | 234/1/24 | 189/0/1 |
| `cal.holiday_behaviour` | old c1/s1 | 6/1/1 | 6/1/1 | 6/0/0 | 6/0/0 | 6/0/3 | 4/2/2 | 6/0/2 | 6/0/1 | 1/0/6 | 2/0/5 | 0/0/9 | 1/0/6 |
|  | **new c2/s2** | 4/4/0 | 2/6/0 | 2/5/0 | 5/2/0 | 7/2/0 | 4/4/0 | 8/0/0 | 7/0/0 | 8/0/0 | 8/0/0 | 8/0/1 | 7/0/0 |
| `cal.missing_open_data` | old c1/s1 | 188/57/13 | 251/7/0 | 257/1/0 | 251/6/0 | 230/28/0 | 212/45/1 | 187/70/2 | 191/66/1 | 255/3/0 | 241/15/1 | 208/51/0 | 189/1/0 |
|  | **new c2/s2** | 188/57/13 | 253/5/0 | 258/0/0 | 254/3/0 | 231/27/0 | 213/45/0 | 187/71/1 | 192/66/0 | 255/3/0 | 239/17/1 | 209/50/0 | 188/2/0 |
| `tick.duplicates_diff_price` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.duplicates_exact` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.nonpositive_crossed` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.ordering` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.rate_anomalies` | old c1/s1 | 100/142/16 | 239/19/0 | 256/2/0 | 249/8/0 | 155/101/2 | 135/121/2 | 101/150/8 | 107/146/5 | 226/32/0 | 208/47/2 | 136/121/2 | 180/10/0 |
|  | **new c2/s2** | 100/143/15 | 240/18/0 | 257/1/0 | 250/7/0 | 155/102/1 | 135/122/1 | 101/151/7 | 107/147/4 | 226/32/0 | 207/49/1 | 136/121/2 | 179/11/0 |
| `tick.spikes` | old c1/s1 | 64/191/3 | 2/115/141 | 14/100/144 | 34/41/182 | 0/4/254 | 0/21/237 | 0/117/142 | 0/119/139 | 3/148/107 | 10/231/16 | 4/72/183 | 0/86/104 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 257/2/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |
| `tick.spread_outliers` | old c1/s1 | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 249/5/5 | 257/1/0 | 258/0/0 | 257/0/0 | 257/2/0 | 183/7/0 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 249/5/5 | 257/1/0 | 258/0/0 | 257/0/0 | 257/2/0 | 183/7/0 |
| `tick.stale_quotes` | old c1/s1 | 153/11/94 | 239/5/14 | 250/6/2 | 243/4/10 | 175/12/71 | 144/35/79 | 135/1/123 | 143/4/111 | 237/3/18 | 213/6/38 | 176/0/83 | 181/1/8 |
|  | **new c2/s2** | 258/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 258/0/0 | 258/0/0 | 259/0/0 | 258/0/0 | 258/0/0 | 257/0/0 | 259/0/0 | 190/0/0 |

Totals by check:

| Check | Old fail | New fail | Old warn | New warn |
| --- | --- | --- | --- | --- |
| `tick.spikes` | 1,652 | **0** | 1,245 | **2** |
| `tick.stale_quotes` | 651 | **0** | 88 | **0** |
| `cal.holiday_behaviour` | 36 | **1** | 4 | 23 |
| `cal.closed_market_ticks` | 37 | **1** | 3 | 23 |
| `cal.gap_location` | 191 | **152** | 49 | 9 |
| `bar.missing_minutes` | 627 | 619 | 49 | 13 |
| `cal.missing_open_data` | 18 | 15 | 350 | 346 |
| `tick.rate_anomalies` | 37 | 31 | 899 | 904 |
| `bar.extreme_returns` | 31 | 31 | 639 | 639 |
| `tick.spread_outliers` | 5 | 5 | 15 | 15 |
| the eight checks that never fired | 0 | 0 | 0 | 0 |

### 9.3 Interpretation — did the accepted changes do what they were meant to do?

**The calendar fixes removed the failures they targeted.** `cal.closed_market_ticks` went from 37
failures to 1 and `cal.holiday_behaviour` from 36 to 1. The 51 `cal.gap_location` failures §4.4
attributed to the calendar are gone; the 152 that remain are 146 export holes at a day boundary
(including 2015-07-01's 165-minute hole, which is not a whole number of hours) and 6 of the
irregular closes C8 chose to leave unmodelled: 2019-07-04 (236.8 min), 2022-01-17, 2023-01-16,
2025-07-04 (91.0 min each, MLK Day and Independence Day ending ~12:58 New York against the 14:30
rule), 2023-11-23 (90.1 min) and 2019-09-02 (77.5 min).

The 23 new warnings on each of the two holiday checks are **a boundary convention, not a calendar
error**: between 1 and 5 ticks landing exactly on the closing instant (17:00:00, 18:00:00 or
18:45:00 UTC), which a half-open `[open, close)` interval counts as outside hours. The four larger
warnings — 2019-11-29 (64 ticks), 2018-02-19 (40), 2019-07-04 (20), 2019-09-02 (10) — are C8
irregulars again.

**One failure is left on each holiday check, and it is the same day: 2024-11-29.** The day after
Thanksgiving closed at **14:43:59 New York** that year against the 13:45 rule C6 set, leaving
7,094 ticks (3.16 % of the day) outside hours. C6 is right for nine of the eleven years in the
window — 13:43–13:45 in 2014, 2015, 2016, 2017, 2018, 2020, 2021, 2022 — and the exceptions are
2019-11-29 (14:05), 2023-11-24 (12:43) and 2024-11-29 (14:43). These three belong to the same
family as C8's irregular dates, which the owner decided to leave unmodelled and let the checks
flag. **A reading for the owner:** either extend C8's list with these three dates so the intent is
written down, or leave it; no calendar change is proposed, and nothing was changed.

**`tick.spikes` now discriminates.** The metric (reverting spike events per million usable ticks)
has a median of 67 across the 3,028 days against a warn level of 2,000, and its yearly medians are
flat — 34 to 117, where the old per-day count ranged from 9 to 228. **Zero days fail, and the only
two days that warn are 2020-03-24 (2,827) and 2020-03-25 (2,532)** — the COVID gold dislocation,
the two days every other check also singles out. T1 and T2 together achieved this: the
per-million unit made the measure comparable across a feed whose rate triples, and the 1 bp floor
removed the bid/ask bounce that made 2018 look pathological. ADR 0069 §1 predicted that these two
days would stop firing on this check because of their high tick count; with the `c2` floor in
place they are instead the top of the distribution, which is a better outcome than the ADR
expected and is recorded here as such.

**`tick.stale_quotes` is now silent, exactly as predicted.** Not merely zero failures: the metric
is **0.0 on all 3,028 days** — no quote anywhere in twelve years repeats unchanged for more than
120 s inside an active session. The 651 old failures were missing data in every case, and the
check is now a genuine frozen-feed detector with nothing to detect.

**The export holes are untouched, as expected**: `bar.missing_minutes` 627 → 619,
`cal.missing_open_data` 18 → 15, `tick.rate_anomalies` 37 → 31. The small falls are the three
Thanksgiving Fridays and a few boundary days the calendar now describes correctly. Nothing but a
re-export can move these.

**The two correctly calibrated checks did not move at all**: `bar.extreme_returns` 31 fail / 639
warn and `tick.spread_outliers` 5 fail / 15 warn, byte for byte as before. That is the right
result — they were measuring the market, and the market did not change.

### 9.4 Evidence — the export-hole inventory for the re-export

From run `01M48QC50R94T5RX5FAW3D4DMT`, counting the recorded 1-minute bar gaps that begin and end
on a whole UTC hour, last one to six hours and cover market minutes under calendar `s2`:
**1,304 holes, 82,620 market minutes, in 103 of the 141 pre-vault months.** 38 months are clean,
among them every month of 2016 and 2017-02 … 2018-01.

| Year | Holes | Market minutes | Months affected | Worst months |
| --- | --- | --- | --- | --- |
| 2014 | 246 | 16,440 | 12 | 2014-10, 2014-03, 2014-04 … |
| 2015 | 19 | 1,140 | 6 | 2015-01, 2015-02, 2015-03 … |
| 2017 | 7 | 480 | 1 | 2017-01 |
| 2018 | 123 | 7,740 | 11 | 2018-10, 2018-09, 2018-12 … |
| 2019 | 157 | 9,600 | 12 | 2019-11, 2019-01, 2019-06 … |
| 2020 | 241 | 15,540 | 12 | 2020-12, 2020-05, 2020-09 … |
| 2021 | 221 | 13,860 | 12 | 2021-11, 2021-08, 2021-06 … |
| 2022 | 33 | 1,980 | 11 | 2022-06, 2022-01, 2022-03 … |
| 2023 | 67 | 4,110 | 10 | 2023-12, 2023-11, 2023-07 … |
| 2024 | 180 | 11,070 | 11 | 2024-04, 2024-10, 2024-07 … |
| 2025 | 10 | 660 | 5 | 2025-03, 2025-08, 2025-01 … |

The 25 worst months, which the re-export script runs first:

| Month | Holes | Market minutes missing | Superseded raw file |
| --- | --- | --- | --- |
| 2014-10 | 70 | 5,520 | `583c06b72da3bd70` |
| 2020-12 | 31 | 2,100 | `88e4f6466b30eb77` |
| 2020-05 | 30 | 1,980 | `14dcca04a08023a9` |
| 2024-04 | 30 | 1,980 | `3148c91ce3641589` |
| 2021-11 | 31 | 1,920 | `82e4404b4f643936` |
| 2023-12 | 28 | 1,740 | `e583b38520e279bf` |
| 2020-09 | 26 | 1,680 | `cc221d62ae55bf5d` |
| 2024-10 | 26 | 1,620 | `2c014bb4c03c8830` |
| 2014-03 | 26 | 1,560 | `af74e9a2b6e33dbe` |
| 2014-04 | 25 | 1,500 | `9fc14c15b9480c24` |
| 2021-08 | 24 | 1,440 | `0cda44a606f0944d` |
| 2021-06 | 23 | 1,440 | `58b644b565a08c26` |
| 2020-08 | 22 | 1,380 | `7804eb0e5fdfa404` |
| 2021-03 | 20 | 1,380 | `90f4778c385df539` |
| 2024-07 | 22 | 1,350 | `94741dae21bc2b60` |
| 2014-02 | 22 | 1,320 | `97271a6cce3cb6a7` |
| 2020-03 | 21 | 1,320 | `71387f90587db928` |
| 2021-07 | 21 | 1,320 | `0222f9e1a892d85d` |
| 2018-10 | 21 | 1,260 | `57a785f52cb4f620` |
| 2024-02 | 21 | 1,260 | `2a7c3fe93e204f97` |
| 2024-05 | 21 | 1,260 | `32294e0b578a4d6c` |
| 2020-01 | 20 | 1,260 | `329fd80802b54aa1` |
| 2019-11 | 19 | 1,260 | `31dd9fa42ab05ecf` |
| 2020-04 | 18 | 1,200 | `29008213f9c77927` |
| 2024-08 | 19 | 1,140 | `b7d9b956d32c268e` |

### 9.5 Limitations of Phase 1

- **The vault is not assessed.** The inventory covers pre-vault months only, so 2025-10 and
  2025-11 are absent from the re-export list although they were downloaded with the same flags and
  are presumably damaged in the same way. 2025-12 … 2026-09 are not downloaded at all.
- A hole and a genuine one-hour venue outage are still indistinguishable from the data; the
  re-export is the test. A month that comes back with the same hole is evidence that the hour has
  no data at the vendor.
- The hole count here (1,304 holes, 82,620 market minutes) is measured from bar gaps under
  calendar `s2`; §5.3's figure (1,203 holes, 75,720 minutes) was measured from tick gaps under
  `s1`. The two definitions differ at day boundaries and on the holiday hours the calendar now
  describes correctly, so the numbers are not directly comparable; neither is wrong.
- `tick.rate_anomalies` still grades early 2014 against a thin hour-of-week norm, and
  `tick.duplicates_*` is still uninformative with one format ingested.
- No returns analysis, EDA, dataset build, regime cut or model run was performed, and no strategy
  result exists. The thresholds were not re-tuned: ADR 0069 fixed them and this run was graded
  against them unchanged.

### 9.6 Action

- Done: the `c2` / `s2` rebuild, the re-grading, the comparison above, and
  `data/reexport.zsh` — the re-export of the 103 damaged months, worst first, with
  `-r 10 -rp 2000 -re -fr`, **written but not run** (the owner runs it).
- Open: Phase 2 — ingest each re-export that improves on the file it replaces (`--supersedes`),
  re-clean and re-bar the affected days, re-run `validate`, fill `config/exclusions.yaml` under
  the 20 % rule from the run that follows, and close DQ-008 if every remaining failure is
  explained.
- A reading for the owner, repeated: the three irregular days after Thanksgiving (2019-11-29,
  2023-11-24, 2024-11-29) are the only calendar-shaped failures left. They can join C8's
  unmodelled list or stay as they are.
