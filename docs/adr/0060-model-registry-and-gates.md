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

## MREG-002 — gate records and enforced transitions

1. **Gate results** (migration 0012, `xq.registry.gates.record_gate_result`): one append-only row
   per evaluation of a gate on a subject, with the plan's columns (subject, gate, criteria,
   values, passed, evaluator, evidence paths, time) plus the evidence policy's hash and the run
   that produced the evidence.
2. **Whether a result passed is computed, never supplied.**
   - Every criterion of the gate (from `GatesConfig.criteria`) must appear as a check, as not
     evaluated or as not applicable. One that appears nowhere is recorded as missing.
   - It passes only when every check passes and nothing is not evaluated or missing.
   - Not applicable is accepted only for the owner's rules (C-25: R2 `pbo_max` and the parameter
     neighbourhood). Anything else is refused as an error, not recorded.
   - A check of another gate, an unknown criterion or a criterion listed twice is refused.
3. **Transitions** (`promote`, `retire`): one step up the promotion order, with the subject's
   **latest** result of the matching gate passing:

   | To | Gate |
   | --- | --- |
   | candidate | R1 |
   | validated | R2 |
   | vault_passed | R3 |
   | paper | R3 (the gate that moves a candidate to paper trading) |
   | live_eligible | R4 |

   - The latest result decides, so a later failure withdraws an earlier pass.
   - `live` is refused: it needs the GATE-004 human review (the plan's "not yet").
   - Retiring needs no gate, from any status but retired.
   - Every change appends a history row naming the gate result that allowed it.
4. **The database enforces the same rules.** The status trigger of `model_versions` is replaced
   by one that checks the step and the latest gate result (the SQL is generated from the same
   step and gate tables in the migration). A test tries every (from, to) pair of statuses, with
   and without a passing result, and checks that a direct `UPDATE` succeeds exactly when the
   service's rules allow it.
5. **What this does not stop.** Anyone with write access to the database file can insert a
   passing gate row by hand, or drop a trigger. The registry makes promotion without evidence
   impossible through its code and visible in its tables (every result names its evaluator,
   evidence and policy hash), not impossible for an administrator. The CLI has no command that
   records a gate result by hand.

## MREG-003 — content-hashed strategy bundles

1. **Content** (`xq.registry.bundles.StrategyBundle`): the model versions pinned by id and
   artifact SHA-256, the feature-set version, the source, instrument, base timeframe and price
   basis, the strategy's configuration, the whole risk profile, and the cost-model version (venue
   and configuration hash, as backtest records carry it). For a rule baseline the strategy
   configuration is the rule and its volatility target, and there is no model version.
2. **Identity is the content.** The bundle id is the SHA-256 of the canonical JSON (sorted keys,
   no whitespace): the same inputs give the same id, any changed input another bundle.
   Registering the same content again returns the bundle already registered (its first name and
   origin stand).
3. **Origin is provenance, not content**: the run and strategy a bundle was built from are stored
   with it but are not hashed. They are where the gate evaluator finds the evidence (GATE-001).
4. **Immutable, and checked on load.** Migration 0013's triggers refuse content edits and
   deletions, and apply the same status steps and gates as model versions. `load_bundle`
   re-hashes the stored content and refuses a bundle whose content no longer matches its id
   ("runtimes load only bundles"; PAPER-001 will load through it). A test shows a bundle edited
   after an administrator dropped the trigger is refused.
5. **What can be bundled now.** `bundle_from_board_run` bundles a rule baseline of a board run,
   with the configured risk profile and cost model. A forecast-sign strategy needs registered
   model versions (ML-009) and is refused. A signal-engine strategy (SIGNAL-004) will add a
   strategy kind when a candidate exists.
6. **CLI.** `xq registry register --run <board run> --strategy <rule>`, `list`, `show` (the
   content checked against its id, the gate results and the history), `promote --to <status>`
   and `retire`. Every change records its `--actor` and `--reason`. There is no command that sets
   a status or records a gate result directly.

## MREG-005 — the active bundle of each environment, and rollback

1. **An append-only pointer** (migration 0014, `active_bundles`): each row activates a bundle in
   an environment or rolls back, naming the bundle active before it, the actor and the reason.
   The latest row of an environment is its active bundle; the whole history stays.
2. **Status decides where a bundle may run**: `paper` needs paper, live_eligible or live; `prod`
   needs live (so nothing can be active in prod until GATE-004). The service checks it, and an
   insert trigger refuses a row whose bundle's status does not allow the environment.
3. **Rollback restores the exact previous bundle** (`xq registry rollback --env paper`): the same
   id, and repeated rollbacks walk further back through the history. A bundle retired since it was
   active cannot be rolled back to, and `load_active_bundle` refuses an active bundle whose status
   no longer allows the environment.
4. **Hot reload.** The runtime does not exist yet (PAPER-001). The policy it must follow is
   `may_switch(flat, at_bar_boundary)`: switch to a newly active bundle only when flat or at the
   next bar boundary, never in the middle of handling one.
5. **Open point for the owner.** The plan's decision point says that if no bundle passes R2,
   Sprints 14–16 validate the infrastructure "using a baseline bundle in paper mode". Under these
   rules a bundle reaches paper only through R1, R2 and R3. Running an ungated baseline bundle on
   the paper infrastructure would need a separate, clearly labelled environment (for example
   `paper_infra`, whose results are never evidence). That is the owner's decision; it is not
   built.
