# ADR 0014 — Dataset specifications and dataset identity

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DS-001 (used by DS-005, DQ-007, EXP-003)

## Context

The plan asks for a pydantic `DatasetSpec` (source, instrument, base timeframe, price basis,
window, context timeframes, feature and target sets with versions, external series, exclusions,
vault policy) and `dataset_id = hash(spec + code version of builders)`. A dataset id must pin
exactly which data, code and configuration produced a result. The spec a researcher writes cannot
know two things in advance: which bar build the stores hold and which quality run gated the
partitions.

## Decision

1. **Everything in the spec is identity.** The id hashes the whole spec, including its name, so
   "same spec" has one meaning: equal after validation. Two specs that differ only in name get
   different ids; that duplication is harmless and avoids a second notion of equality.
2. **Canonical form before hashing.** Timestamps are converted to UTC, durations are parsed
   (`"2D"`, `"P2D"` and `"48h"` are equal), context timeframes are sorted by length and
   exclusions by trading day, then the spec is dumped as sorted-key JSON without whitespace. YAML
   key order and formatting cannot change an id.
3. **Resolved specs only.** A spec carries `bar_build` and `quality_run_id`. The builder fills
   them when it resolves the spec against the stores (the configured bar build; the quality run
   that gated the partitions). `dataset_id` refuses an unresolved spec, and the resolved spec is
   what is stored with the dataset, so rebuilding from the stored `spec.yaml` reproduces the id.
   A researcher may pin either field in the YAML; resolution refuses to change a pinned value.
4. **Code versions.** `dataset_id` also hashes `code_versions()`: `DATASET_CODE_VERSION` for the
   builder, extended by the feature and target builders as they are added. Changing builder
   logic without bumping its version is a bug.
5. **Restrictions for now.** `vault_policy` accepts only `exclude`: research datasets never
   contain vault data. `external_series` must be empty until an external-data task defines how
   external series get their `available_at`. Context timeframes must be longer than the base
   timeframe.
6. **Id format.** `ds-` followed by the first 16 hex digits of SHA-256 (64 bits), the same width
   as the config hash.

## Consequences

- The same YAML can resolve to a different id after new bars are built or a new quality run is
  made. That is intended: the id changes because the data may have changed.
- Rebuilding a stored dataset from its `spec.yaml` must reproduce both the id and the content
  hash; DS-005 checks this.
