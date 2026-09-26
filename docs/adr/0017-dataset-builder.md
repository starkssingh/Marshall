# ADR 0017 — Dataset builder, storage and the base feature set

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DS-005 (with DS-001 identity, ADR 0014; DQ-007 adds the quality gate)

## Context

DS-005 materializes features and targets separately to `data/datasets/<id>/` with a manifest (row
count, range, SHA-256, git sha, spec, quality run ids, excluded partitions), and a rebuild must
reproduce the SHA-256. The feature library (FEAT-001, `feature_sets` table) is Sprint 7, but
Sprint 3's working system needs datasets now.

## Decision

1. **Rows and decision time.** One row per *complete* base bar with `start <= bar_start < end`;
   the decision time is the bar's `available_at`. Incomplete bars (the export ended inside them)
   and bars of excluded trading days are dropped before anything is computed. `warmup` history is
   loaded before `start` for trailing computations and then dropped.
2. **Reads go through the catalog.** The builder reads bars only with `Catalog.load_bars`, so the
   vault cutoff applies, and calls `check_window` first so a window past `vault.start` fails
   before any work. Context bars are loaded from one context bar before the warm-up, so a context
   value is available from the first row.
3. **Resolution.** The builder pins the configured bar build (a different pinned value is
   refused), the quality run (the pinned run, or the latest run of the source on that bar build
   that overlaps the window and did not include vault days; none is an error pointing at
   `xq validate`), and a digest of the calendar and instrument configuration. The resolved spec is
   written to `spec.yaml` and is what the id is computed from.
4. **Code versions in the id.** `dataset_id(spec, code)` hashes the dataset builder's code version
   and the code version of every feature (and, from TGT-001, target) set the spec uses.
5. **Base feature set until FEAT-001.** `base.v1` (`xq.datasets.base_features`) carries the
   decision bar's own values (OHLC of the spec's price basis, tick count, spread mean/max/close,
   flagged and excluded tick counts), each context timeframe's latest available bar
   (`ctx_<tf>_open` … `ctx_<tf>_tick_count`) with its provenance column `ctx_<tf>_available_at`,
   and, from DS-007, calendar columns. Nothing is engineered or fitted. It lives in a new module
   next to the plan's `datasets/` files; FEAT-001 will move feature definitions into
   `xq.features` with the `feature_sets` table and keep `base.v1` reproducible.
6. **Storage.** `features.parquet` (zstd) holds `decision_time_utc` and the feature columns;
   timestamps are Arrow `timestamp[ns, tz=UTC]`, which is int64 nanoseconds in UTC physically and
   allows missing values (a context bar may not exist yet). Loaders return a tz-aware
   `decision_time` index.
7. **Integrity and reproducibility.** The manifest stores each file's SHA-256 and a combined hash.
   `load_dataset` and `verify_dataset` re-hash files before use. Building an existing id rebuilds
   into a staging directory and compares: identical content is a no-op (the stored dataset and
   its manifest are kept); different content, or a stored file that no longer matches its
   manifest, raises `DatasetIntegrityError`. Datasets are published by renaming the staging
   directory, so a failed build leaves nothing half-written.
8. **Registry row.** `dataset_versions` (migration 0004) records the id, name, resolved spec and
   its hash, feature and target set versions, bar set ids, quality run ids, window, row count,
   combined SHA-256, git sha, path and creation time.
9. **CLI.** `xq dataset build <spec.yaml>` and `xq dataset show <id>`;
   `experiments/configs/ds_base.yaml` is the base research spec (15m mid bars, 1h/4h/1d context,
   about four years ending at `vault.start`; its start must be checked against the broker's real
   history depth).

## Consequences

- A dataset id pins the data (bar build), the gating (quality run), the configuration the builder
  reads (digest) and the code (versions). Rebuilding from `spec.yaml` reproduces the id and bytes
  in the same locked environment.
- Parquet bytes depend on the pyarrow version; a lockfile change can change file hashes without
  changing values. The rebuild check reports that as an integrity error, which is the safe side.
