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

The rebuild under the DQ-008 decisions (clean rules `c2`, calendar `s2`) was measured on the same
machine in C-36, from the raw store with nothing re-downloaded: clean 18 m 41 s, build-bars
3 m 49 s, spread-stats 1 m 15 s, validate 2 m 44 s. A clean store is about 6.9 GB and a bar set
about 0.43 GB, so a rules or calendar change costs that much again until the retired store is
removed — `clean` re-checks each partition file on disk, so deleting a retired store's files is
safe and its manifest rows can stay as the record (`data/removed_stores.log`).

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
ls data/raw/dukascopy/xauusd/2014/10/        # e.g. 583c06b72da3bd70__XAUUSD_2014-10.csv
uv run xq ingest --source dukascopy --path data/downloads/reexport/XAUUSD_2014-10.csv \
  --supersedes 583c06b72da3bd70 \
  --reason "re-export: whole-hour holes"
uv run xq verify-raw                          # both files are still checked
uv run xq clean --source dukascopy            # the affected trading days are rebuilt
uv run xq build-bars --source dukascopy
uv run xq spread-stats --source dukascopy
uv run xq validate --source dukascopy
```

**In bulk.** C-36 found 103 of the 141 pre-vault months damaged (1,304 whole hours, 82,620 market
minutes; DQ-008 review §9.4) and wrote `data/reexport.zsh` for them: worst first, ten retries per
hour with a 2 s pause (`-r 10 -rp 2000 -re -fr`), into `data/downloads/reexport/`, a month already
re-exported skipped, a failed month's partial file deleted, and a clean stop if free space falls
below 5 GB. It is not committed (`data/` is git-ignored) and the owner runs it:

```bash
caffeinate -i zsh data/reexport.zsh 2>&1 | tee data/reexport.log
```

Re-export only, then ingest month by month: `--supersedes` takes one file per call, and a
re-export is worth ingesting **only if it has fewer missing market minutes than the file it
replaces** — otherwise keep the old file. Budget disk space for both versions of every month
re-exported, or free it by deleting the superseded files' Parquet mirror parts, which are derived
data that nothing reads once the supersession is recorded.

`--path` must be the one re-exported file. The ingest is refused, with nothing stored, without a
reason, for an unknown or already superseded raw file id, for a file of another source, for a
byte-identical file, or for a file whose ticks do not overlap the old file's period. Back up
`data/raw` again afterwards: it now holds both versions of the month.

### Filling the exclusion list (ADR 0071)

After the damaged months are re-exported and the quality run is repeated, a trading day may be
excluded from datasets **only if more than 20 % of its calendar market minutes are still missing**:
the `cal.missing_open_data` metric of that day in the new run. One-hour holes stay warnings.

1. List the candidates from the run's results (`quality_results`, check `cal.missing_open_data`,
   `metric_value > 0.2`), and set aside any day where the calendar, not the data, is at fault.
2. Add each day to `config/exclusions.yaml` under its source with `trading_day`, a `reason` naming
   the defect, its `missing_market_share` (the metric) and the run's `quality_run_id`. The file is
   the only place the list can be set; an entry at or below 20 % does not load.
3. Commit the change with the run id in the message. Every dataset build checks each listed day
   against its own gating run and refuses an entry the run does not support.

## 3. What a real-data session may do now

Until the quality run repeated under the DQ-008 decisions exists and the exclusion list is filled
from it (C-36), nothing runs on real data beyond the pipeline above, re-exports and
`xq validate` (see "Not allowed yet" in `docs/STATUS.md`).

The C-8 session has run: the whole 2014-01 … 2025-11 download was ingested, cleaned, barred and
graded, and the review is in [`docs/data/quality-review-2026-10.md`](../data/quality-review-2026-10.md).
The owner decided its proposals (C-35): ADR 0069 (thresholds, clean rules `c2`), ADR 0070 (the
calendar, version `s2`) and ADR 0071 (re-export supersession, the exclusion list).

**The C-36 session** applies them, in this order:

1. `uv sync`, then rebuild the whole history under `c2` and `s2` — `xq clean`, `xq build-bars`,
   `xq spread-stats` (step 2's commands). The rules version changes, so these write a new clean
   store and new bar builds next to the old ones (about 21 minutes for clean and bars, measured
   under `c1`); budget the disk space.
2. Re-export the damaged months (review §7.3; 2014-10 first) and ingest each with `--supersedes`
   ("Re-exporting a damaged month" above); `xq clean` and `xq build-bars` again.
3. `xq validate`, and compare the new run with `01M47ZDA2E631VVD7703MXWMQN` check by check.
4. Fill `config/exclusions.yaml` from the new run ("Filling the exclusion list" above).
5. Back up `data/raw`.

The March 2024 observation this session was told to start from — "the last Friday tick on
2024-03-01 was at 20:59:59 UTC, an hour before the calendar's weekly close" — was **wrong**: the
tick was at 21:59:59.793 UTC, 16:59:59 New York EST, one second before the close. Over 595
weekends the close is 16:59 New York in both DST regimes (review §4.2).
