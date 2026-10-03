# ADR 0060 — The model registry and the release gates (Sprint 13)

- **Status:** accepted
- **Date:** 2026-09-28
- **Decided by:** Claude, within the plan (Phase 19, Phase 25, Sprint 13) and the owner's
  instruction to build MREG-001 … MREG-005 and GATE-001 … GATE-003 on synthetic data. Open points
  are flagged for the owner's review.
- **Tasks:** MREG-001, MREG-002, MREG-003, MREG-005, MREG-004, GATE-001, GATE-002, GATE-003

Everything here is tested on synthetic data only. No bundle has been gated on real data, and the
vault has never been opened.

## Context

Phase 19 asks for versioned models and deployable strategy bundles whose status transitions are
only possible through recorded gate passes ("nothing should reach paper or live trading on
someone's say-so"); Phase 25 for the gate evaluator, the one-time vault procedure and the human
review. Its failure conditions are mutable bundles and manual database edits to a status.

## MREG-001 — models, model versions and statuses

1. **Schema** (migration 0011): `models` (name, task, description), `model_versions` (the plan's
   columns: artifact URI with its SHA-256, dataset, feature-set version, target, training window,
   hyperparameters, metrics snapshot, git sha, run, status) and `status_history` (subject kind and
   id, from, to, the gate result that allowed it, actor, reason, time). One `status_history`
   serves model versions and strategy bundles (MREG-003).
2. **Statuses** (`xq.registry.models.Status`): draft → candidate → validated → vault_passed →
   paper → live_eligible → live, and retired from any status but retired. A version is registered
   as draft, with its first history row.
3. **Immutability is in the database.** SQLite triggers refuse any update of a version's content,
   any deletion of a model or a version, and any update or deletion of the history. Until MREG-002
   the only status change they allow is retiring. A refit is a new version (`version` = latest +
   1).
4. **SQLite only, on purpose.** The triggers are SQLite's; the migration refuses another database
   rather than create the tables without their enforcement. PostgreSQL's versions come with the
   move to PostgreSQL (PAPER-004).
5. `status_history.gate_result_id` is a plain integer, not a foreign key: `gate_results` arrives
   in the next migration, and adding a key to an SQLite table rebuilds it, which would drop its
   triggers.

No forecasting model exists yet (ML-009 is not built), so model versions are exercised by tests
only; the first registered subjects are strategy bundles.
