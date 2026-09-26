# ADR 0005 — Raw store layout, file identity and the Parquet mirror

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DATA-004

## Context

The raw store is the root of provenance: every bar and every result must trace back to the exact
bytes a vendor delivered (development plan, Phase 1). It must be immutable, re-ingest must be a
no-op, and rebuilding derived data from it must be reproducible.

## Decision

1. **Identity is content.** A raw file is identified by the SHA-256 of its bytes (unique in
   `raw_files`); `raw_file_id` is the first 16 hex digits of that hash. Ingesting the same bytes
   again — under any name, from any path — is skipped. Rebuilding the store from the same files
   yields the same ids, so derived data keyed by `raw_file_id` is reproducible.
2. **Layout.** Originals are copied to
   `data/raw/<source>/<instrument>/<yyyy>/<mm>/<raw_file_id>__<original name>`, where `yyyy/mm` is
   the UTC month of the file's first row (`undated/` for files without rows). The copy's hash is
   verified and the file is made read-only (0444).
3. **Everything reads the stored copy.** The file is parsed from its copy in the raw store, not
   from the caller's path, so the mirror and the manifest describe exactly the immutable bytes.
4. **Parquet mirror.** A faithful copy of each file — every row, in file order, with the original
   timestamp text, typed values, `raw_file_id`, `row_num`, and `ts_utc`/`ts_flags` from the
   source's declared clock — at
   `data/raw_parquet/<source>/<instrument>/year=YYYY/month=MM/day=DD/part-<raw_file_id>.parquet`,
   split by UTC day. The mirror is derived and can be rebuilt; it is not read-only.
5. **The manifest row is the commit point.** A file counts as ingested only when its `raw_files`
   row exists (one transaction per file). An interrupted run leaves at most a stored copy without a
   row; the next run adopts it if its hash matches and refuses with `RawStoreIntegrityError` if it
   does not.
6. **Integrity is checkable.** `xq verify-raw` re-hashes every manifest file and reports missing,
   modified and writable files. Permission bits do not stop root, so verification — not the mode
   bits — is the guarantee.
7. **Schema upgrades.** `xq ingest` brings the SQLite metadata database to the latest migration
   before ingesting (research tier convenience); `xq db upgrade` does the same explicitly.

## Consequences

- Downstream stages (cleaning, bars) can be keyed on `raw_file_id` and rebuilt deterministically.
- A re-exported file with a single changed byte is a new raw file; overlapping exports therefore
  coexist in the raw store and overlap is resolved in cleaning (DATA-007), never by editing files.
- The mirror depends on the source's clock convention, which is why a source's clock may not
  change once it has files (ADR 0003).
