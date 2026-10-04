# ADR 0062 — Owner review of the Dukascopy session, Sprint 13 and C-15 (C-26, C-27, C-28)

- **Status:** accepted
- **Date:** 2026-10-04
- **Decided by:** project owner, reviewing the open points of the Dukascopy session (C-26,
  ADR 0057), Sprint 13 (C-27, ADR 0060) and the C-15 session (C-28, ADR 0061); amends ADR 0057,
  ADR 0060 and ADR 0061
- **Tasks:** DATA-013, MREG-002, GATE-002, GATE-004, BASE-005, ARCH-004

Nothing in this session runs on real data. The owner's own run of the data pipeline on one month
of real Dukascopy ticks (decision C-26 (1)) is recorded here as the owner reported it; Claude has
not seen its output.

## C-28 — the C-15 readings are confirmed

1. **Where each record is judged.** The H-0001 board test (Sharpe p-value, DSR, random-entry null,
   slices) uses each rule's full history after its warm-up; `xq validate-strategy` (R1, R2), the
   registry's `backtest` history (MREG-004) and the vault's walk-forward interval (R3) keep judging
   a rule on its fold-aligned record. Confirmed as built.
2. **The first evaluation day** counts from the first evaluation decision (the rule is flat
   before it). Confirmed as built.
3. **Donchian waits for its exit channel** (only `exit > max(entry, atr_window)` changes).
   Confirmed as built.

No code changes.

## C-26 — the Dukascopy session

1. **The CSV route is the working route.** On 2026-10-04 the `.bi5` endpoint answered HTTP 503 on
   the owner's machine. The dukascopy-node CSV route worked end to end on March 2024 (one CSV of
   125 MB, 21 trading days); the full pipeline ran on it (`xq ingest`, `clean`, `build-bars`,
   `spread-stats`, `validate`), and `xq validate` graded 328 checks pass, 25 warn and 3 fail
   (read in the C-8 session, not here).
   - The documented command was wrong: `-re` alone aborts the export on the empty weekend hours.
     The working flags are `-r 3 -re -fr` (`-fr`: do not fail the export after the retries are
     spent). The README carries the corrected command.
   - The owner's monthly download loop (one CSV per month; the partial file deleted when an export
     fails; `caffeinate` to keep the Mac awake; a log) is the documented procedure:
     `docs/runbooks/real-data.md`.
   - `xq fetch dukascopy` (the `.bi5` downloader) stays as built, for the day the endpoint answers
     again. Dukascopy's JSON API is **not** added to `xq fetch`: the CSV route answers the open
     question.
2. Files stored as the vendor's bytes, one per hour, named after the UTC hour; no file for an
   empty hour — **approved**.
3. Canonical tick sizes stay NaN; volumes are kept in the raw mirror — **approved**.
4. The download pace of `xq fetch dukascopy` — **approved**.
5. The calendar stays unchanged until `xq validate` on real data shows Dukascopy's hours —
   **approved**. See the observation below.
6. **`ds_base.yaml` starts on 2015-01-01**, fixed now, before any result. The owner downloads from
   2014-01-01 so the year before the start is available as warm-up.
   - The spec's `start` is `2015-01-01T22:00:00Z`, the start of trading day 2015-01-02 (17:00 New
     York, EST). Trading day 2015-01-01 is New Year's Day, closed by the calendar, so this is the
     first trading day of 2015 and the same data as a start at midnight; it keeps the spec's
     convention of starting at a trading-day start. `end` stays `vault.start`.
   - The discovery-window default (`eda.discovery.fraction` 0.5, `end` still unset) now resolves
     from 2015-01-01: the first half of the span to `vault.start`, ending at the start of trading
     day 2020-05-15 (`2020-05-14T21:00:00Z`). It becomes `eda.discovery.end` when the owner fixes
     it from the real data's depth (C-16).
   - The H-0001 draft names ds_base's window (2015-01-01 to `vault.start`) in its header and
     statement. Its two window fields stay unset: registering it is the owner's step (C-15).
   - **A reading for the owner (C-29).** Under ADR 0061 a rule's warm-up is counted on the
     dataset's own signal bars, so the 2014 data serves the dataset's `warmup` (10 days of
     history before `start`), the sigma-hat warm-up and the quality report, but not the board's
     rule warm-ups: `tsmom_252@1d` (253 daily bars) is first evaluated about a year after the
     start, `ma_crossover_50_200@1d` after about 200 trading days. Letting rules warm up on the
     2014 bars would need the board to read signal bars before the dataset's start (a change to
     the board runner and its ADR).
7. `--source` defaults to `data.primary_source` — **approved**.

**Observation for DQ-004 / DQ-008 (the calendar is not changed now).** In the owner's March 2024
data, Dukascopy XAUUSD's last tick on Friday 2024-03-01 was at 20:59:59 UTC, an hour before the
calendar's weekly close at 22:00 UTC (17:00 New York, EST). Whether Dukascopy's Friday close is
an hour early every week, only in winter, or only that day is for DQ-004 on the full download and
the DQ-008 review; the calendar stays as it is until then (decision 5).

## C-27 — Sprint 13

1. A bundle reaches paper only through R1, R2 and R3; if no bundle passes R2, the paper
   infrastructure is exercised with a baseline bundle in a separate, labelled environment (for
   example `paper_infra`), never counted as evidence — **approved**; built with Sprint 14 if it is
   needed.
2. A failed vault evaluation spends the bundle's one vault access; any exception needs an ADR —
   **approved**.
3. R3 on the screening tier — **approved, with a new requirement: promotion to `paper` requires R3
   recomputed on the event tier with the real risk engine.** Enforced in the transition rule:
   - every gate result records its **evidence tier**, `screening` (the default: the vectorized
     screener, risk limits read from daily losses against the profile) or `event` (the event
     backtester, with the risk engine's own decisions);
   - `promote(..., Status.PAPER)` needs the subject's latest R3 result to have passed **and** to be
     on the event tier; a screening-tier R3 (such as the vault evaluation's) still promotes to
     `vault_passed`;
   - the database enforces the same rule (migration 0016 adds `gate_results.evidence_tier` and
     replaces both status triggers).
   The event-tier R3 evaluation itself is not built: no candidate exists to run on the event tier.
   Until it is, no subject can reach `paper`.
4. Enforcement in code and SQLite triggers, visible but bypassable by an administrator with write
   access to the database file — **approved**.
5. Promotion to `paper` is gated by R3, the same gate as `vault_passed` — **approved**, as amended
   by (3): the R3 result it reads must be an event-tier one, so in practice a later R3 than the
   vault's.
6. **GATE-004 records the SHA-256 of the signed GATE-003 review document.** The sign-off stays a
   document; when GATE-004 (the live-readiness checklist) is built, it records the document's
   SHA-256 so the reviewed text is identified. Noted in STATUS as a GATE-004 requirement.
7. Only rule bundles can be bundled and vault-evaluated until ML-009 — **approved**.

## Logging of third-party libraries

Third-party loggers (matplotlib, PIL, fontTools and the like) are set to WARNING when
`configure_logging` runs, so CLI output is not flooded with matplotlib's `findfont` debug lines.
The list and the level are configuration (`logging.third_party` and `logging.third_party_level`
in `config/base.yaml`); the project's own `xq` loggers keep `logging.level`.

## Where real-data sessions run

Real-data sessions run in Claude Code on the owner's Mac, where the data is: `data/` is
git-ignored and never leaves that machine. Cloud sessions such as this one work on synthetic data
only.

## Consequences

- C-28 is closed. C-26 and C-27 are decided; their code changes are in this session's commits.
- C-29 is opened: whether the board's rule warm-ups may read signal bars before the dataset's start
  (the 2014 data).
- No subject can be promoted to `paper` until an event-tier R3 evaluation exists.
