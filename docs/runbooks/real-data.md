# Runbook: real Dukascopy data on the owner's machine

Real-data sessions run in Claude Code on the owner's Mac, where the data is (ADR 0062). `data/` is
git-ignored: market data is never committed and never leaves that machine. Cloud sessions work on
synthetic data only.

## 1. Download: one dukascopy-node CSV per month

The `.bi5` endpoint behind `xq fetch dukascopy` answered HTTP 503 on 2026-10-04; the
dukascopy-node CSV route is the working route (ADR 0062, C-26 (1)). It was tested end to end on
March 2024: one CSV of 125 MB, 21 trading days, and the full pipeline below ran on it
(`xq validate`: 328 checks pass, 25 warn, 3 fail).

The flags that work are `-r 3 -re -fr`:

- `-r 3`: three retries per failed hour;
- `-re`: retry hours that come back empty;
- `-fr`: do not fail the export once an hour's retries are spent. With `-re` alone the export
  aborts on the empty weekend hours.

An hour skipped after its retries is missing from the CSV. `xq validate`'s gap checks report
missing market hours; export that month again if one appears.

**This happens often enough to matter.** In the 2014-01 … 2025-11 download, 1,203 whole hours are
missing inside market hours — 1.81 % of the pre-vault market minutes — and they are recognisable
because each gap begins and ends within five seconds of a whole UTC hour and lasts one, two or
three hours (`docs/data/quality-review-2026-10.md` §5.3). 2016 has none, so a clean export of this
feed is possible; 2014-10 lost 205 hours and is the worst month in the window. Raising `-r` above
3 and re-exporting any month that `xq validate` reports holes in is worth the time.

The owner's loop downloads one month per export, from 2014-01 (the year before `ds_base`'s start
on 2015-01-01, for warm-up) to the last complete month. A month whose CSV already exists is
skipped, so the loop can be stopped and started again. When an export fails, its partial file is
deleted, so a half-written month is never ingested. `caffeinate` keeps the Mac awake, and every
step is logged:

```bash
# From the repository root. Months are YYYY-MM; LAST is the last complete month.
caffeinate -i bash -s <<'EOF'
FIRST=2014-01
LAST=2026-09
OUT=data/downloads/csv
LOG="$OUT/download.log"
mkdir -p "$OUT"

next_month() {  # YYYY-MM -> the following YYYY-MM (no GNU/BSD date differences)
  local y=${1%-*} m=${1#*-}
  m=$((10#$m + 1))
  if (( m > 12 )); then m=1; y=$((y + 1)); fi
  printf '%04d-%02d' "$y" "$m"
}
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

month=$FIRST
stop=$(next_month "$LAST")
while [[ "$month" < "$stop" ]]; do
  next=$(next_month "$month")
  name="XAUUSD_$month"
  if [[ -f "$OUT/$name.csv" ]]; then
    echo "$(stamp) skip $name (already downloaded)" >> "$LOG"
  else
    echo "$(stamp) start $name" >> "$LOG"
    if npx dukascopy-node -i xauusd -from "$month-01" -to "$next-01" -t tick -f csv -v \
         -bs 5 -bp 1000 -r 3 -re -fr -dir "$OUT" -fn "$name" >> "$LOG" 2>&1 < /dev/null; then
      echo "$(stamp) done $name" >> "$LOG"
    else
      rm -f "$OUT/$name.csv"
      echo "$(stamp) FAILED $name (partial file deleted)" >> "$LOG"
    fi
  fi
  month=$next
done
EOF
```

Follow it with `tail -f data/downloads/csv/download.log`, and run it again until no month says
`FAILED`. `-to` is exclusive, so each export covers its whole month. Keep the default UTC offset
(`-utc 0`). Do not ingest `.bi5` files and CSVs for the same period: the second copy of every tick
would be flagged `DUP_EXACT` (ADR 0057).

This loop is the owner's procedure, written down from the owner's description. In the cloud
sandbox it was run against a stub `npx` only (the month and year roll-over, skipping a month
already downloaded, deleting a failed month's partial file, the log); dukascopy-node itself cannot
be reached from there.

## 2. The data pipeline

Each step can be re-run safely; raw files are immutable and every step is logged.

```bash
uv sync
uv run xq ingest --source dukascopy --path data/downloads/csv
uv run xq clean --source dukascopy
uv run xq build-bars --source dukascopy
uv run xq spread-stats --source dukascopy
uv run xq validate --source dukascopy        # reports/quality/<run id>/report.md (pre-vault)
```

`xq validate` grades every ingested trading day before the vault (`--start` and `--end` narrow
it). Vault days are never read without a gate token.

Measured on the full 143-month download (520,973,737 ticks, 24.85 GB of CSV) on the owner's Mac:
ingest 24 m 51 s, clean 16 m 39 s, build-bars 4 m 35 s, spread-stats 1 m 21 s, validate 3 m 48 s.
Budget disk space before starting: the stores come to about **1.87×** the CSV bytes — the raw copy
is verbatim (1.00×), plus the zstd Parquet mirror (0.55×), the clean store (0.30×) and the bars
(0.02×).

**`data/raw` is the only copy of the market data.** The downloaded CSVs were deleted after ingest,
once `xq verify-raw` and an independent re-hash of both copies of all 143 months confirmed that
each CSV, its read-only raw copy (mode 0444) and its `raw_files` manifest row carry the same
SHA-256; every deletion is logged with size and digest in `data/deleted_csvs.log`. The raw store is
not backed up by this repository (`data/` is git-ignored) — **backing it up is the owner's step**.
`xq verify-raw` re-hashes the whole store in about 17 s and should be run before relying on it.

### Re-exporting a damaged month (ADR 0071)

A month the downloader left whole-hour holes in is re-exported with the same command as in step 1
and ingested as a **new raw file that supersedes the old one**. Nothing in the raw store is
deleted or rewritten; the manifest records which file replaced which, when and why, and clean,
bars and `xq validate` read only the replacement from then on.

```bash
# The old file's raw_file_id is the prefix of its stored name (data/raw/<source>/<instrument>/...).
ls data/raw/dukascopy/xauusd/2014/10/        # e.g. 0123456789abcdef__XAUUSD_ticks_2014-10.csv
uv run xq ingest --source dukascopy --path data/downloads/reexport/XAUUSD_ticks_2014-10.csv \
  --supersedes 0123456789abcdef \
  --reason "re-export of 2014-10: the first export skipped 205 whole UTC hours (DQ-008 review 5.3)"
uv run xq verify-raw                          # both files are still checked
uv run xq clean --source dukascopy            # the affected trading days are rebuilt
uv run xq build-bars --source dukascopy
uv run xq spread-stats --source dukascopy
uv run xq validate --source dukascopy
```

`--path` must be the one re-exported file. The ingest is refused, with nothing stored, without a
reason, for an unknown or already superseded raw file id, for a file of another source, for a
byte-identical file, or for a file whose ticks do not overlap the old file's period. Back up
`data/raw` again afterwards: it now holds both versions of the month.

## 3. What a real-data session may do now

Until the DQ-008 review is signed off, nothing runs on real data beyond the pipeline above and
`xq validate` (see "Not allowed yet" in `docs/STATUS.md`).

The C-8 session has run: the whole 2014-01 … 2025-11 download was ingested, cleaned, barred and
graded, and the review is in [`docs/data/quality-review-2026-10.md`](../data/quality-review-2026-10.md).
It found the trading boundaries correct and the holiday early closes wrong; its calendar and
threshold proposals wait for the owner (ADR 0069, status "proposed"). The calendar is not changed
before the owner decides.

The March 2024 observation this session was told to start from — "the last Friday tick on
2024-03-01 was at 20:59:59 UTC, an hour before the calendar's weekly close" — was **wrong**: the
tick was at 21:59:59.793 UTC, 16:59:59 New York EST, one second before the close. Over 595
weekends the close is 16:59 New York in both DST regimes (review §4.2).
