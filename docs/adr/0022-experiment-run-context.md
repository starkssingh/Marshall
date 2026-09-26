# ADR 0022 — Experiment run context

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** EXP-003 (on ADR 0020 and ADR 0021)

## Context

EXP-003: `with experiment_run(hypothesis_id, config) as run:` captures git sha (refusing a dirty
tree unless `--exploratory`, which marks the run non-confirmatory), config hash, dataset id,
seeds, `uv.lock` hash, host and timings, and logs metrics and artifacts. CLAUDE.md: every research
run goes through the run context; confirmatory runs require a clean git tree.

## Decision

1. **Signature.** `experiment_run(cfg, engine, hypothesis_id, config, *, kind, seed, dataset_id,
   exploratory, title)`; configuration and database are explicit, like everywhere else.
2. **Confirmatory is the default and is strict.** A confirmatory run starts only if the commit is
   identifiable (a git repository), the tree has no uncommitted or untracked changes, and
   `uv.lock` exists. Otherwise it raises before recording anything. `exploratory=True` records a
   non-confirmatory run, with `+dirty` appended to the sha when the tree was dirty, so it can never
   be mistaken for citable evidence.
3. **What is captured.** Git sha; a 16-hex config hash over the run's configuration *and* the
   application config hash (both stored); the dataset id, verified against its manifest before the
   run starts; the SHA-256 of `uv.lock`; the seed (all randomness is seeded through
   `xq.core.seeds`, and the context offers a seeded generator); the host; start and finish times
   and the final status.
4. **Experiments open implicitly.** A run joins the latest open experiment on the hypothesis's
   current version, creating one if needed, so an edited hypothesis starts a new experiment.
5. **Failures are recorded, not hidden.** If the block raises, the run is marked `failed` and the
   exception propagates; failed runs stay in the registry (ADR 0020).

## Consequences

- Research code has one way in, and whatever it produces can be traced to code, data,
  configuration, environment and seed.
- Writing outputs inside the repository dirties the tree; outputs belong under the git-ignored
  `data/` and `reports/` directories.
