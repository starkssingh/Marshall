# ADR 0039 — Experiment conclusions and the research log

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** EXP-005 (on EXP-001 … EXP-003)

## Context

EXP-005: closing an experiment requires a verdict (supported, rejected, inconclusive) and the
Observed / Evidence / Interpretation / Limitations / Action fields; an entry is appended to
`docs/research/log.md`. Acceptance: closing without a conclusion fails. Phase 18's research
validation: an audit lists experiments without conclusions, and the list must be empty at the end
of each sprint. The schema has a `conclusions` table (plan Phase 18).

## Decision

1. **Schema.** Migration `0008` adds `conclusions(experiment_id, verdict, observed, evidence,
   interpretation, limitations, action, created_at)`, one row per closed experiment. The
   `experiments` row gets status `closed`, the verdict and `closed_at` in the same transaction.
2. **One way to close.** `xq.tracking.conclusions.close_experiment` is the only function that
   closes an experiment, and it needs a `ConclusionDoc`: a verdict and five fields that may not be
   empty or blank. It refuses an experiment already closed (conclusions are never rewritten; a new
   view is a new experiment), an experiment with a run still running, a `supported` verdict
   without a finished **confirmatory** run (exploratory runs are never evidence, EXP-003) and a
   `rejected` verdict without any finished run. `inconclusive` needs no run (an abandoned
   experiment is closed honestly as inconclusive).
3. **Research log.** The entry — hypothesis id, version and title, verdict, the experiment and
   every run (kind, confirmatory or exploratory, status, git sha, dataset) and the five fields — is
   appended to `paths.research_log` (`docs/research/log.md`, committed) before the database commit,
   so a failed write leaves the experiment open. Entries are appended, never edited: the log in
   git is the durable record across machines, while the metadata database is local.
4. **CLI.** `xq exp close <experiment_id> --conclusion <yaml>` (template:
   `experiments/conclusions/TEMPLATE.yaml`) and `xq exp audit`, which lists the experiments
   without a conclusion and exits 1 if there is any.

## Consequences

- An experiment cannot quietly end without a written verdict, and a "supported" claim always
  points at a confirmatory run.
- The sprint-end audit is one command. No experiment has been closed yet: nothing has run on real
  data.
