# ADR 0071 — DQ-008: re-exported months supersede raw files; the exclusion list

- **Status:** accepted
- **Date:** 2026-10-06
- **Decided by:** the project owner (C-35, items 3 and 4 of the DQ-008 review's open questions);
  the mechanisms implemented by Claude
- **Tasks:** DQ-008, DATA-004, DATA-007, DQ-007
- **Refines:** ADR 0005 (the raw store is immutable), ADR 0012 / DQ-007 (the quality gate's
  exclusions)
- **Related:** ADR 0069 (thresholds), ADR 0070 (calendar)

Synthetic data only in this session. Nothing was fetched, re-exported or excluded; the list is
empty.

## Context

The DQ-008 review found 1,203 whole UTC hours missing from the download (1.81 % of pre-vault
market minutes), the signature of dukascopy-node's `-fr` skipping an hour whose retries were spent
(review §5.3). The remedy is a re-export of the damaged months, then an exclusion list for days
that are still damaged. But the raw store is immutable (ADR 0005): a re-exported month has
different bytes, so ingesting it adds a second raw file for the same period, and clean would read
both — every tick the two share flagged `DUP_EXACT`, the hole's ticks present once. The review
also proposed 15 days for exclusion (§7.5), which the dataset spec could only list one by one.

## Decision

### 1. A re-export supersedes the earlier raw file(s), explicitly

- **Raw data stays immutable.** A re-exported file is ingested as a **new raw file** that names
  the raw file(s) it **supersedes**, with a **reason**: `xq ingest --path <file> --supersedes
  <raw_file_id> [--supersedes ...] --reason "..."` (`ingest(..., supersedes=, reason=)`).
  Nothing is deleted, moved or rewritten.
- **Recorded in the manifest.** Table `raw_file_supersessions` (migration 0018): the superseded
  and superseding raw file ids, the source, the superseded file's period (first and last tick,
  UTC), the reason, the ingest run and when. It is written in the same transaction as the new
  file's `raw_files` row; the ingest run's parameters carry the request too.
- **Clean and bars use the superseding file and never mix both.** Every stage that reads raw
  files (`build_clean`, the bar build's coverage, the quality run's coverage) reads only the
  **active** files (`active_raw_files`: those no supersession names). A trading day's clean
  partition records the raw file ids it was built from, so a supersession changes them and the
  day is rebuilt on the next `xq clean`; a trading day that straddles a month boundary is built
  from the neighbouring month and the re-export, never the superseded file.
- **`xq verify-raw` still covers both files**: it re-hashes every manifest row, superseded or not.
- **Refused, with nothing stored:** no reason; not exactly one new file at the path; an unknown
  raw file id, one of another source, or one already superseded (supersede the file that replaced
  it instead — a chain is allowed); a new file byte-identical to a stored one; a new file whose
  period (first to last tick) does not overlap the superseded file's. A re-export that covers less
  than the file it replaces is allowed but logged (`superseding_file_shorter`): the old file's
  ticks outside it are no longer read.

### 2. The exclusion list: config-driven and documented

- `config/exclusions.yaml` (`AppConfig.exclusions`, file-only like the gates: no profile,
  environment variable or override can change it) lists, per source, the **trading days excluded
  from every dataset**, each with a **reason** and its **evidence**.
- **The rule (approved): exclude a day only if more than 20 % of its calendar market minutes are
  still missing *after* the re-export.** One-hour holes (about 4 % of a 23-hour day) stay
  warnings in the dataset manifest. The share is the metric of `cal.missing_open_data` (whole
  market-hours minutes without a 1-minute bar) in a quality run graded after the re-export.
- **The list is not filled in this session.** The owner's local session fills it from the
  evidence of that quality run. The review's 15 proposed days were measured *before* the
  re-export and are not carried over.

### 3. How the exclusion list is applied (implementation)

- `ExclusionsConfig` (`rule`, `days` per source). The rule's `check` is fixed to
  `cal.missing_open_data` and `max_missing_market_share` is 0.20. Each entry needs a
  `trading_day`, a non-empty `reason`, its `missing_market_share` — which **must exceed** the rule
  or the configuration does not load — and the `quality_run_id` that measured it. A day is listed
  once per source; a source must be configured. `AppConfig.excluded_days(source)` returns the
  list.
- The dataset builder adds the listed days within the span a dataset reads to its exclusions
  (warm-up included), with the reason `exclusion list (config/exclusions.yaml): <reason>`; a reason
  the spec gives itself for the same day takes precedence. They appear in the manifest's
  `excluded_partitions` with their failing checks, like any DQ-007 exclusion.
- **Evidence check** (`check_exclusion_evidence`): before excluding, the builder reads the gating
  quality run's `cal.missing_open_data` metric for each listed day; a day graded at or below the
  rule refuses the build (`QualityGateError`). A day the run did not grade (no data at all) is not
  contradicted.
- The list enters the dataset's config digest — only when it names a day for the source, so
  datasets built while it is empty keep their ids — so filling it changes the ids of the datasets
  it affects.
- Known truth: `tests/unit/core/test_exclusions_config.py` (the repository list is empty under
  the 20 % rule; 20 % and 4.3 % are refused, 43.5 % accepted; reason and evidence required; a day
  listed twice, another check or an unknown source refused; the list cannot come from a profile,
  an override or an environment variable) and `tests/integration/datasets/test_exclusion_list.py`
  (a listed day is dropped from the features, recorded in the manifest and changes the id; a day
  outside the dataset is ignored; a spec's own reason wins; a gating run at the rule refuses the
  build).

## Consequences

- Re-exporting a damaged month is a routine, auditable step: ingest with `--supersedes`, then
  `xq clean`, `xq build-bars`, `xq spread-stats` and `xq validate`. Both versions of the month stay
  in the store and its backups; which one is used, and why, is in the manifest.
- A superseded file's mirror parts stay on disk; they are derived data and are simply not read.
- The quality run graded after the re-export (and under ADR 0069 and ADR 0070) is the evidence
  for the exclusion list and the run DQ-007 gates datasets on. Filling the list is a commit that
  names that run; every entry is checked against it again at every dataset build.
- Known truth: `tests/integration/data/test_supersession.py` — a month exported with a missing
  hour and its re-export: after the supersession the damaged day is rebuilt from the re-export
  alone (the hole filled, no `DUP_EXACT`), the boundary day from the re-export and the next month;
  the supersession row carries reason, run and period and nothing leaves the manifest;
  `verify_raw_store` passes on both files and detects tampering with the superseded one; every
  malformed request is refused with nothing stored; a file is superseded once, a chain is allowed,
  another source's file is refused; the CLI path works end to end.
