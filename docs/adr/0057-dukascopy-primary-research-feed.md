# ADR 0057 — Dukascopy ticks are the primary research feed; the project is data-only for now

- **Status:** accepted
- **Date:** 2026-09-28
- **Decided by:** project owner (decisions 1 and 6); Claude, within them (decisions 2 to 5, for
  the owner's review, C-26)
- **Supersedes:** ADR 0004's choice of the primary feed. Its MT5 adapter stays, as an optional
  source.
- **Tasks:** DATA-013 (promoted from an optional secondary feed to the primary feed); DATA-011
  later

## Context

The development plan (section 1, assumption 1) makes the execution broker's own bid/ask ticks the
primary research feed. Dukascopy is allowed only as an optional secondary long-history source
(DATA-013), stored separately and never spliced into broker bars. ADR 0004 declared a placeholder
MT5 source, `mt5_primary`, clock `NY+7`, until the owner named a broker. No broker has been named,
so no real data exists: C-8 is blocked, and C-17 (whether to build DATA-013) was open.

The owner decided:

- the project is **data-only for now**: there is no execution venue;
- the **primary research feed is Dukascopy's XAUUSD bid/ask ticks** (UTC timestamps, history
  from 2003);
- **OANDA v20** (bid/ask S5 candles) is a later cross-check (DATA-011) and a candidate live feed;
- **costs stay placeholders** until an execution venue exists.

This conflicts with the plan's feed choice. By `CLAUDE.md`, the plan wins on architecture and
the project instructions on research standards; a feed choice is the owner's decision (plan,
section 1), and the conflict is recorded here. The plan's reason for a broker feed — bars,
spreads and timestamps that match the venue traded — no longer holds. Any result is conditional
on Dukascopy's quotes until DATA-011 measures the basis against a venue.

## Decision 1 — the `dukascopy` source is primary

- `config/base.yaml` declares the source `dukascopy`: adapter `dukascopy_ticks`, vendor
  "Dukascopy Bank SA", feed type `vendor_ticks`, venue `dukascopy`, price type `bid_ask_ticks`,
  clock `UTC`, instrument `xauusd`, `vendor_symbol` XAUUSD, `point_scale` 1000.
- `data.primary_source: dukascopy` is the source the pipeline commands (`ingest`,
  `rebuild-mirror`, `clean`, `build-bars`, `spread-stats`, `validate`) read without `--source`.
  `experiments/configs/ds_base.yaml` names it.
- `mt5_primary` stays declared, unchanged, as an optional source (ADR 0004).
- Dukascopy data is its own source id. It is never merged into another source's bars (plan,
  DATA-013).

## Decision 2 — what the adapter reads

**Native hourly `.bi5` files.** One LZMA "alone" stream per instrument and UTC hour. Once
decompressed, it holds 20-byte big-endian records `>i4 >i4 >i4 >f4 >f4`: milliseconds from the
start of the hour, ask points, bid points, ask volume, bid volume.

- A price is its points divided by `point_scale`: 1000 for XAUUSD, so 2034155 is 2034.155.
- A tick's time is the hour start plus its offset, converted through the declared clock like any
  other source's.
- An empty file is an hour without ticks.
- The vendor's URL numbers months from 00:
  `…/XAUUSD/2024/02/11/13h_ticks.bi5` is 11 March 2024, 13:00 UTC.
- These facts come from the source of dukascopy-node v1.46.4, the widely used open-source
  client: its decompressor, normaliser, URL generator and instrument metadata. That metadata
  gives XAUUSD a decimal factor of 1000 and a first tick at 2003-05-05T00:01:03.421Z. The vendor
  itself could not be reached from the sandbox.

**File names carry the hour.** The bytes do not say which hour they cover, and the raw store
keeps a file's name but not its folder (ADR 0005). Each file is therefore named
`<SYMBOL>_<YYYY-MM-DD>_<HH>h_ticks.bi5`, with a 1-based month. The adapter refuses a file that:

- has another name form or another symbol;
- is not an LZMA stream;
- decompresses into a partial record;
- holds an offset outside `[0, 3 600 000)` ms.

**dukascopy-node CSV** is the fallback (decision 5): header
`timestamp,askPrice,bidPrice[,askVolume,bidVolume]`, timestamps in Unix ms, which is the default,
or ISO 8601 text. An ISO cell may be naive (read in the declared clock) or carry a zero offset.
Any other offset is refused.

**Nothing is invented.** Every row carries both sides of the quote, so there is no
carry-forward; a missing side is flagged `MISSING_QUOTE`. Rows keep their file order, and the
canonical ticks are sorted stably by UTC time (an out-of-order row is flagged, as for MT5).

**Volumes** stay in the raw frame and the Parquet mirror (`ask_volume`, `bid_volume`, plus the
integer points of `.bi5` rows). Canonical `bid_size` and `ask_size` stay NaN: the unit of
Dukascopy's gold volumes is not documented, and tick volume is a feed artifact (plan,
assumption 1). FEAT-007 stays gated.

## Decision 3 — `xq fetch dukascopy`, the owner's downloader

`xq fetch dukascopy --instrument xauusd --from YYYY-MM-DD --to YYYY-MM-DD --out <dir>` runs on
the owner's machine; the sandbox has no internet access. It stores each UTC hour's `.bi5` bytes
unchanged, so the raw store keeps the vendor's own bytes rather than a conversion of them:
`<out>/XAUUSD/<yyyy>/<mm>/<dd>/XAUUSD_<yyyy-mm-dd>_<HH>h_ticks.bi5`.

- **Checksummed.** `<out>/XAUUSD/manifest.jsonl` gets one line per hour: `ok` with the file's
  SHA-256, size and record count, or `empty`; the URL, HTTP status and fetch time. A payload is
  kept only if it decodes (decision 2).
- **Resumable.**
  - Recorded hours are re-hashed and not requested again; a mismatch or a missing file stops the
    run, and nothing is repaired.
  - A file without a manifest line, left by a run stopped between the two writes, is adopted
    after a decoding check.
  - A partial last manifest line, left by a run stopped mid-write, is cut off, so its hour is
    fetched again.
  - A lock file keeps a second download out of the same folder.
- **Never overwrites.** A payload goes to a hidden `.part` file and is hard-linked to its name
  only if the name is free. On file systems without hard links, an exclusive create is used
  instead.
- **Polite.**
  - One request at a time, at least 0.5 s apart.
  - Network errors, timeouts, HTTP 429 and 5xx are retried with backoff (2 s, 4 s, 8 s) up to
    4 attempts; then the run stops with the fallback named.
  - Any other HTTP error stops the run at once.
  - Settings are in `sources.dukascopy.download`.
- **Empty answers are suspect inside market hours.** A 404 or an empty body is an hour without
  ticks. An empty hour inside the calendar's market hours is handled like this:
  - it is asked again once, after 5 s;
  - it is recorded only once a later hour in the run brings ticks, proving the feed was
    answering;
  - if the run ends before such an hour, it is left unrecorded and the next run asks again;
  - 24 such hours in a row stop the run.

  A failing endpoint must not be recorded as a silent market. `--retry-empty` asks again for
  hours already recorded empty.
- **Range limits.** `--to` must be before today (UTC), since a day in progress is incomplete, and
  `--from` may not be before 2003-05-05.
- **No new dependency:** `urllib` and `lzma` are in the standard library.
- **No layout change.** The downloader is `src/xq/data/adapters/dukascopy_fetch.py`, inside the
  plan's `data/adapters/` layout. The CLI gains the group `xq fetch`.

## Decision 4 — the calendar is re-checked against Dukascopy, and unchanged for now

The owner asked for the sessions and calendar assumptions to be re-checked against Dukascopy's
trading hours. Dukascopy publishes its market hours and a trading-breaks calendar (in GMT) on
dukascopy.com, but this sandbox's network policy blocks that domain, so they could not be read.
The table compares each assumption with what is known of the feed. Each open point has a check
that settles it on real data (DQ-004, `xq validate`), and changes wait for that evidence and the
DQ-008 review. The synthetic fixtures use the calendar's own model, so they test the adapter and
the clock, not Dukascopy's real hours.

| Assumption (ADR 0002, `config/sessions.yaml`) | Dukascopy | What changes, and how it is confirmed |
| --- | --- | --- |
| Timestamps in broker server time `NY+7` (ADR 0003, ADR 0004) | UTC: the vendor's hour files are UTC hours | Clock `UTC`: no repeated or skipped wall times, so no `TS_DST_*` flags. The market's New York-anchored gaps move an hour in UTC with US DST; the files' hours do not. Tested on fixtures across both 2024 changes; `cal.gap_location` on real data |
| Trading day rolls at 17:00 New York | — | Unchanged: the platform's fixed convention |
| Market hours: Sunday 18:00 to Friday 17:00 New York, daily break 17:00–18:00 New York | Not read (blocked). Retail gold quotes usually pause at the New York rollover, but venues differ by minutes | Unchanged. Ticks inside the break are flagged `CLOSED_MARKET` (`cal.closed_market_ticks`); a shifted or shorter break fails `cal.gap_location`; a later open shows in `cal.missing_open_data` |
| Holidays: NYSE calendar; closed on New Year's Day, Good Friday and Christmas; early close at 13:30 New York on other NYSE holidays and on 24 and 31 December | A Swiss bank with its own trading-breaks calendar: it may quote through some US holidays or pause on others | Unchanged. `cal.holiday_behaviour` reports each holiday on real data; a difference becomes a config change with an ADR after DQ-008 |
| One schedule for every year | The history starts in 2003. Early years' hours, liquidity and tick density likely differ from today's | Read the quality report per year before the owner fixes the dataset window. Early years may need exclusions (DQ-007) rather than a different calendar |
| Weekend | Every hour is requested, weekends included | Stray weekend ticks are kept and flagged `CLOSED_MARKET` |
| Sessions (Tokyo, London, New York), LBMA, COMEX and US-release anchors, event windows | Defined in local time; independent of the feed | Unchanged |
| Rollover and financing at 17:00 New York | No execution venue: Dukascopy's own swaps are irrelevant | Unchanged; placeholder costs |
| Spread statistics (and the cost model's p90 spread fallback) | SWFX's spreads, not a retail venue's | Unchanged. The spread statistics describe the feed, not future execution; costs stay placeholders |
| Quality thresholds (`config/quality.yaml`, provisional, ADR 0013) | Set with a broker tick feed in mind; Dukascopy's tick density is unknown | Reviewed in DQ-008 on the first real report |

## Decision 5 — the `.bi5` endpoint may be down; the CSV route is the fallback

dukascopy-node issue #254 (7 July 2026) reports that `datafeed.dukascopy.com` stopped answering:
the TLS handshake completes, then nothing is sent until a timeout. The project moved to a JSON API
in versions 1.47–1.50 (July 2026), and a later comment says the old endpoint still times out.
Whether it works now could not be checked from the sandbox.

- `xq fetch dukascopy` targets the `.bi5` endpoint, as the owner asked. The URL is configurable,
  and the downloader stops after four failed attempts on one hour, naming the fallback.
- The fallback: the owner runs dukascopy-node, which now uses the JSON API, and exports one tick
  CSV per month with Unix-ms timestamps and volumes. The `dukascopy` source reads those files
  (decision 2); the README gives the command.
- The CSV route has no manifest or no-overwrite guarantee of its own. The raw store still hashes
  every file at ingest (ADR 0005), and re-exported files with changed bytes coexist and are
  flagged as duplicates in cleaning.
- The two formats should not be ingested for the same period. The second copy of each tick would
  be flagged `DUP_EXACT` and left out of bars, but it would still count in the quality checks.

**Owner decision if the endpoint stays down:** keep the dukascopy-node CSV route, or have Claude
add Dukascopy's JSON API to `xq fetch`. That would store native JSON per hour, with the same
manifest, resume and politeness guarantees and a JSON reader in the adapter.

## Decision 6 — costs, venue and later feeds

- There is no execution venue. The cost model stays `placeholder` (ADR 0029, ADR 0032), and every
  net result stays "screening, placeholder costs".
- OANDA v20 bid/ask S5 candles come later, as the DATA-011 cross-check and a candidate live feed
  (a vendor-bar path, DATA-012). Nothing is built for them now.

## Consequences

- **Data volume.** The full history is about 205,000 hourly requests, more than a day at the
  default pace. The download can run in pieces.
- **C-17 is closed:** DATA-013 is built, as the primary feed. **C-8 now means:** the owner
  downloads the history, runs `xq ingest`, `xq clean` and `xq build-bars`; the next session
  runs `xq validate` on at least a year of real Dukascopy ticks; then the DQ-008 review.
- **Provisional broker assumptions.** The unnamed broker and its `NY+7` clock now concern only
  the optional `mt5_primary` source.
- **Dataset window.** Dukascopy's history no longer limits the ~4-year window of `ds_base.yaml`
  (start 2021-09-26). A longer window is the owner's decision, taken before any result. The
  H-0000 and H-0001 windows, "set from the real data's depth at registration", are now set from
  Dukascopy's depth.
- **Feed basis.** A future venue's quotes will differ from Dukascopy's. DATA-011 measures the
  basis before any paper trading, and FEAT-007 stays gated.
- **Content identity** (ADR 0005). Two hours with byte-identical files would be one raw file,
  and the second would be skipped at ingest (logged). The downloader writes no file for an empty
  hour. Two non-empty hours would need the same ticks at the same millisecond offsets with the
  same volumes, which is not expected in real data.
