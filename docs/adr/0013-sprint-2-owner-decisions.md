# ADR 0013 — Owner decisions on the Sprint 2 open questions

- **Status:** accepted
- **Date:** 2026-09-26
- **Decided by:** project owner, in answer to the open questions in the Sprint 2 pull request
- **Refines:** ADR 0006 (drops), ADR 0009 / 0011 (spread buckets), ADR 0011 (thresholds),
  ADR 0012 §3 (vault handling in `xq validate`)

## Context

The Sprint 2 pull request ended with open questions to the owner: whether to ratify the proposed
quality thresholds, how to treat rollover spread widening, whether to drop exact duplicates by
default, whether `xq validate --include-vault` is acceptable, and what to do while the broker and
its calendar are unnamed. This ADR records the answers so later work does not reopen them.

## Decision

1. **Quality thresholds are ratified as provisional.** Every threshold in `config/quality.yaml`,
   including those ADR 0011 marks "proposed", is accepted as it stands. The thresholds may change
   **once**, through a new ADR, after the DQ-008 human review of a quality report on real broker
   data. They may **never** change after any strategy result exists. Once the first strategy
   result is recorded in the experiment registry (Sprint 4 onward), `config/quality.yaml`
   thresholds are frozen, like `config/gates.yaml`.
2. **Rollover spreads.** Spread statistics and the spread-outlier check keep New York
   hour-of-week buckets (ADR 0009, ADR 0011). Whether to use 15-minute buckets is revisited in the
   DQ-008 review, with real data to show how much of the rollover widening an hourly bucket
   hides. The `SPREAD_OUTLIER` cleaning flag keeps its trailing-median definition until then.
3. **Exact duplicates.** Current behaviour stays: the clean store keeps `DUP_EXACT` ticks,
   flagged (`cleaning.drop` stays empty), and bars exclude them through `bars.exclude_flags`.
4. **Vault-period quality validation belongs to the release gate.** `xq validate --include-vault`
   is not used before the release gate; vault-period validation runs inside the GATE-002
   procedure. The flag stays, but:
   - it refuses to run unless `--i-understand-vault-access` is also given (library callers must
     pass `vault_access_confirmed=True`), and raises `VaultAccessError` otherwise;
   - every confirmed use logs a `vault_validation_access` warning naming the run, the source and
     the vault trading days validated, and the run stays marked `includes_vault` in
     `quality_runs`.
   When GATE-002 builds the one-time token flow, this confirmation will be replaced by a gate
   token.
5. **Broker and calendar stay pending.** `mt5_primary` (clock `NY+7`) stays a placeholder and no
   broker facts are assumed. The calendar defaults (ADR 0002) stay unconfirmed. Sprint 2 stays
   "implemented and tested, not validated" until `xq validate` has produced a quality report on at
   least one year of real broker ticks before `vault.start`, and DQ-008 has reviewed it.

## Consequences

- DQ-007 (Sprint 3) gates datasets on the ratified thresholds. A dataset built before the DQ-008
  review records the quality run it used, so a later threshold change (the one allowed) shows up
  as a different gate decision rather than silently re-grading old datasets.
- Nobody, human or agent, can validate vault days by accident: the flag needs a second,
  deliberately worded confirmation, and every use leaves a warning in the logs.
- Research may proceed on synthetic data, but no result is described as validated on real data
  until item 5 is met.
