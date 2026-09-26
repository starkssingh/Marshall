# ADR 0001 — Record architecture decisions

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** ARCH-001

## Context

The development plan (`docs/specs/development-plan.md`) fixes the architecture and the build order,
but many smaller decisions will be made while implementing it: data conventions, library choices,
deviations from the plan, and the reasons for thresholds. Research results are only trustworthy if
the decisions behind them are traceable, and several rules in the plan (gate thresholds, quality
thresholds, research assumptions) may only change through a recorded decision.

## Decision

Architecture and research-methodology decisions are recorded as Architecture Decision Records in
`docs/adr/`, one Markdown file per decision, numbered sequentially (`NNNN-short-title.md`).

Each ADR contains:

- **Status:** proposed, accepted, superseded by ADR NNNN, or deprecated.
- **Date** and the **backlog task IDs** it relates to.
- **Context:** the forces at play and the problem.
- **Decision:** what was decided, stated so it can be checked.
- **Consequences:** what becomes easier, harder, or must be revisited.

Accepted ADRs are not edited except to change their status; a changed decision gets a new ADR that
supersedes the old one.

An ADR is required for: any deviation from the development plan; any change to the module layout;
any change to `config/gates.yaml` or `config/quality.yaml` thresholds; any change to a research
assumption (cost model, horizon, window, universe, vault); and any decision the plan explicitly
defers to an ADR.

## Consequences

Decisions become reviewable and reversible deliberately rather than by drift. The cost is a short
document per decision.
