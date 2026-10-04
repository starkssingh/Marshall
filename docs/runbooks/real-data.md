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

## 3. What a real-data session may do now

Until the DQ-008 review, nothing runs on real data beyond the pipeline above and `xq validate`
(see "Not allowed yet" in `docs/STATUS.md`). The C-8 session reads the quality report per check
and per year, compares the calendar with Dukascopy's real hours (ADR 0057, decision 4) and
prepares the DQ-008 human review. It starts with one observation from March 2024: the last
Friday tick on 2024-03-01 was at 20:59:59 UTC, an hour before the calendar's 22:00 UTC weekly
close (EST) (ADR 0062). The calendar is not changed before that review.
