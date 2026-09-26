# ADR 0020 — Experiment registry schema

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** EXP-001 (EXP-002 to EXP-004 build on it)

## Context

Phase 18 lists the registry tables: `hypotheses`, `experiments`, `runs`, `trials`, `metrics`,
`artifacts` and `conclusions`. Multiple-testing corrections (DSR, SPA) need an honest trial
count, and results must be reproducible from what the registry records. EXP-001's acceptance is
"CRUD tests; migrations".

## Decision

1. **Tables (migration 0005).** As in the plan, with these additions: `hypotheses` keeps the
   title and the exact YAML text of each version (so superseded versions stay readable) and is
   keyed by (id, version); `experiments` records the hypothesis *version* it tests; `runs` records
   the run's configuration as JSON and the host, next to git sha, config hash, dataset id, lock
   hash and seed; `metrics` and `artifacts` use surrogate keys (a metric may have no fold).
   `conclusions` arrives with EXP-005, which owns closing experiments.
2. **Append-only.** The API (`xq.tracking.registry`) creates and reads records and moves a few
   fields forward through their lifecycle: a hypothesis version becomes `superseded` when a new
   version is added; a run goes from `running` to `finished` or `failed` exactly once and accepts
   no metrics or artifacts afterwards. There are no delete or edit functions — "CRUD" here stops
   at create, read and lifecycle updates — because deleting a failed run or an unflattering trial
   would corrupt the trial count.
3. **Versioning by text hash.** Adding a hypothesis whose text hash equals the latest version's is
   a no-op; any change creates the next version. Experiments bind the version current when they
   were opened, so an edit after results exist is visible and cannot silently rewrite what was
   tested.
4. **Frozen records.** Functions return frozen dataclasses, not ORM objects, so callers cannot
   mutate registry rows outside the API.

## Consequences

- Everything a run cites (hypothesis version, data, code, configuration, seed, environment) can be
  read back from the registry.
- Mistaken records stay in the registry; they can only be superseded (hypotheses) or marked
  failed (runs), which is the point.
