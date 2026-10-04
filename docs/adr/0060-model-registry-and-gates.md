# ADR 0060 — The model registry and the release gates (Sprint 13)

- **Status:** accepted
- **Date:** 2026-09-28
- **Decided by:** Claude, within the plan (Phase 19, Phase 25, Sprint 13) and the owner's
  instruction to build MREG-001 … MREG-005 and GATE-001 … GATE-003 on synthetic data. Open points
  are flagged for the owner's review.
- **Tasks:** MREG-001, MREG-002, MREG-003, MREG-005, MREG-004, GATE-001, GATE-002, GATE-003
- **Amended by:** ADR 0062 (C-27): promotion to `paper` needs R3 recomputed on the event tier
  (migration 0016); GATE-004 will record the SHA-256 of the signed GATE-003 review

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

## MREG-004 — performance history per bundle

1. **Daily rows** (migration 0015, `bundle_performance`): bundle, source (`backtest`, `vault`,
   `paper`, `live`), trading day, net return on the capital, net P&L, trades closed, and the run
   that produced them. The key is (bundle, source, day).
2. **Appended, never rewritten.** `append_performance` takes the days in order and refuses a day
   at or before the last one recorded for that source; triggers refuse updates and deletions.
3. **Where rows come from.** `xq registry register` appends the origin board strategy's recorded
   out-of-sample returns (verified against their SHA-256) as the `backtest` history; the vault
   evaluation appends `vault` rows (GATE-002). Paper and live rows come with PAPER-004 and
   PAPER-006, which compare them with the Monte Carlo bands. `xq registry history` summarizes
   them per source.
4. The board records daily returns only, so a board strategy's `backtest` rows have no trade
   counts.

## GATE-001 — `xq gate evaluate <bundle>`

1. **Wired to `xq validate-strategy`.** The evaluator runs a new validation of the bundle's origin
   strategy (a run of kind `validation`; confirmatory on a clean tree), or reads an existing one
   named with `--validation`. An existing one must be a finished validation of exactly the
   bundle's origin run and strategy; anything else is refused.
2. **The validation's report is evidence, so it is checked.** `report.json` must match the SHA-256
   the registry recorded. Every check is rebuilt against `config/gates.yaml`: a validation judged
   under other thresholds or comparisons is refused ("validate again"), never reinterpreted, and a
   recorded outcome that disagrees with its own value is refused.
3. **R1 and R2 gate results** are recorded from it on the bundle, with the validation run, the
   report paths and the evaluator's identity. Whether each passed is computed by MREG-002: an
   incomplete verdict (a criterion not evaluated) never passes. R3 is recorded by the vault
   evaluation, and R4 needs paper trading.
4. **The report** (`reports/gates/<bundle>/<time>/gate.md` and `gate.json`) lists the plan's ten
   release-gate items (Phase 25) with their status (pass, fail, not evaluated, not applicable,
   pending) and detail; the dataset's quality evidence (its quality runs, excluded and WARN
   partitions, for item 1); the reproduction status of the origin run (EXP-006), reported, not
   gated; and the evidence paths.
5. **The evaluator never promotes.** Promotion is a separate, recorded decision (`xq registry
   promote`) that reads the latest gate results.
6. **Layout.** Phase 25 names commands, not modules. The evaluator is `xq.registry.evaluate` and
   the vault procedure `xq.registry.vault`, beside the plan's `registry/models.py`, `bundles.py`
   and `gates.py`: they read and write the registry and nothing else does. This ADR records that
   addition to the plan's layout.

**Known truth** (`tests/integration/registry/test_gate_evaluate.py`, synthetic ticks, a
confirmatory board run): a rule bundle is registered with its backtest history; the evaluation
runs a confirmatory validation and records R1 and R2, each passing exactly when the validation's
verdict is `pass`; the report has the ten items (data validation pass, vault and paper pending);
the bundle stays draft, and an ungated promotion is refused; a validation of another strategy,
and an altered report, are refused; reading an existing validation adds results without
rerunning it. On three weeks of synthetic ticks both gates fail, as they should on such a sample.

## GATE-002 — the one-time vault evaluation

1. **One token per bundle, ever** (`xq gate vault-token <bundle> --issued-by <name>`). A token is
   issued only to a bundle whose status is `validated` and whose latest R2 result passed, and
   only if no token was ever issued for that bundle: a second issuance is refused whatever
   happened to the first token ("second vault access for same bundle refused", the plan's test).
   The secret is printed once; `vault_tokens` keeps its SHA-256, the issuer and the expiry
   (`vault_procedure.token_ttl_hours`, 24, in `config/validation.yaml`). This closes ADR 0015's
   "no issuance in the library yet".
2. **The evaluation** (`xq gate vault-evaluate <bundle> --token … --quality-run … [--last-day]`)
   is a confirmatory run of kind `vault_evaluation`: a dirty tree is refused, with no exploratory
   option, because its result is evidence.
   - **Checks before the vault is read**, so they cannot spend the token:
     - the token belongs to this bundle;
     - the window ends at the end of a complete trading day (the last complete one by default);
     - the named quality run grades every open trading day of the window (warm-up and vault,
       from the market calendar) with no FAIL day. A run made without `--include-vault` is
       refused for the days it lacks.
   - **The first read redeems the token** for this run (DS-004). Every read is logged as a
     `vault_access_granted` warning and in `vault_access_log`; any other run presenting the token
     is refused.
   - **The rule is evaluated by the board's own code**: the bars of the dataset's source and
     timeframes, loaded through the catalog with the token; the feature set's function computed
     in memory (a vault dataset is never materialized, so no later research run can load it); the
     board's signal bars and rule positions; the screener with the configured cost model and
     sigma-hat. Warm-up history comes from before the vault, decisions and fills from inside it.
3. **R3** is recorded on the bundle from the vault run (`net_sharpe_min`,
   `walk_forward_interval.low/high`, `risk_limit_breaches_max`, `vault_access_logged`):
   - the walk-forward interval: the quantile of the vault's per-period Sharpe ratio among
     stationary-bootstrap resamples of the bundle's backtest history (the walk-forward
     out-of-sample returns, MREG-004), each truncated to the vault's length, under the gates'
     bootstrap convention;
   - risk-limit breaches on the screening tier: days whose loss reaches the risk profile's daily
     loss limit, plus one if the drawdown (capital as the first peak) reaches the halt. The
     screener sizes without the risk engine, so this is a reading of the risk profile, not an
     engine's refusals. The event tier will replace it when a candidate runs there;
   - access logged: 1 when the access log holds this run's reads.

   The vault's daily returns are appended as the bundle's `vault` history. Promotion to
   `vault_passed` then reads R3 like any other gate.
4. **A failed evaluation spends the token.** If the evaluation fails after the vault was read, the
   bundle's vault access is used up; reopening it needs the owner's decision in an ADR. This is
   the conservative reading of "one-time"; the checks before the first read exist so that bad
   inputs fail before it.
5. **Scope.** Only rule bundles from a board run can be evaluated: a bundle of models needs
   ML-009. `xq validate --include-vault` keeps its explicit confirmation for grading vault days
   (ADR 0013): grading data quality is a pipeline stage, not a research read, and the evaluation
   refuses vault days a quality run has not graded.

**Known truth** (`tests/integration/registry/test_vault.py`, synthetic ticks spanning a test
`vault.start` of 18 March 2024): a draft bundle gets no token; a validated one gets one, and a
second is refused; another bundle's token, and a quality run that does not grade the vault days,
are refused with no vault read; the one evaluation is confirmatory, redeems the token, logs five
reads (four bar sets and the ticks), records R3 with its five criteria and appends the four vault
days to the history; a second evaluation with the spent token, and a new token, are refused; the
bundle moves to `vault_passed` exactly when R3 passed. The R1 and R2 results that bring the test
bundles to `validated` are recorded directly from synthetic checks, labelled as such: that module
tests the vault procedure, and the evaluator is tested on its own.

## GATE-003 — the human review template and sign-off

1. **The template** is `docs/specs/gate-review.md` (the plan's deliverable). It names the subject
   (bundle, origin, gate report, validation run, gate results, reproduction status), a checklist
   of the ten release-gate items with their default criteria, the questions a reviewer answers
   (results too good to be true, the honesty of the trial count, every not-applicable and
   not-evaluated criterion and the source behind it, the warnings, placeholder costs,
   pre-registration, the vault's integrity), the decision (the next status only, a rejection, or
   more evidence) and a sign-off table for the reviewer and the owner.
2. **Filled in by the evaluator.** `xq gate evaluate` writes `review.md` next to each gate report,
   with the subject's fields filled in and every checklist box empty: the review is the human's.
3. **The rules it states**: a reviewer may refuse a bundle that passed the gates, never pass one
   that failed them or change a threshold; no LLM, Claude included, makes or signs the decision; a
   promotion names its review in `--reason`.
4. **What is not built.** The signed review is a document, not a database record. GATE-004 (the
   live-readiness checklist) will decide whether a signed review must be recorded before a live
   transition; until then no bundle can reach `live`.

**Known truth** (`tests/integration/registry/test_gate_evaluate.py`): the evaluation writes the
review with the bundle, its status, the validation run and the gate results filled in, ten
unticked checklist rows and an empty sign-off.

## Open points for the owner

- MREG-005: whether an ungated baseline bundle may run on the paper infrastructure for
  infrastructure tests (a separate, labelled environment), as the plan's decision point suggests.
- GATE-002: a failed vault evaluation spends the bundle's vault access; any exception needs the
  owner's ADR.
- GATE-002: R3's risk-limit breaches are read from the screener's daily returns against the risk
  profile, not from the risk engine's refusals, until a candidate runs on the event tier.
- MREG-002: enforcement is in the code and the database's triggers, which an administrator with
  write access to the database file can bypass; every gate result names its evaluator, evidence
  and policy hash so a bypass is visible.
