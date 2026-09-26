# ADR 0021 — Hypothesis pre-registration

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** EXP-002 (on the registry of ADR 0020)

## Context

EXP-002: `experiments/hypotheses/H-XXXX.yaml` with statement, rationale, target, information set,
horizon, timeframe, discovery and evaluation windows, primary metric, success and falsification
criteria, trial budget, planned tests and slices; the file hash is locked on registration and
edits create a visible new version.

## Decision

1. **Schema.** `HypothesisDoc` (pydantic, no extra keys) holds exactly the plan's fields plus a
   title and a `family` (the trial family used by EXP-004 and later by DSR/SPA). Lists that make
   a hypothesis testable — information set, success and falsification criteria, planned tests —
   must be non-empty, and the trial budget positive.
2. **Research-standard checks at registration.** The discovery window must end before the
   evaluation window starts (ideas found by looking at data are tested on later data), and the
   evaluation window must end at or before `vault.start` (the vault belongs to the release gate).
   The file must be named after its id.
3. **Exact-text lock.** The hash is SHA-256 of the file's exact text, not of a normalized
   document: any edit, even whitespace or a comment, becomes a new version. That is deliberately
   strict — "I only reformatted it" is not something the registry should have to trust.
4. **Workflow.** Copy `experiments/hypotheses/TEMPLATE.yaml` (whose placeholder id cannot be
   registered), fill it in before looking at evaluation data, `xq exp register <path>`;
   `xq exp hypotheses` lists every version with its status. Experiments bind the version current
   when they open (ADR 0020).

## Consequences

- The registry can show, for any result, the exact hypothesis text it tested and whether the
  hypothesis was edited afterwards.
- No real hypothesis is registered in Sprint 3; the first, H-0001 (baseline board), comes with
  Sprint 4.
