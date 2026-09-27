# ADR 0041 — Owner answers at the Sprint 5 review

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, reviewing PR #9 (Sprint 5) and the open readings of ADR 0035 and
  ADR 0036
- **Tasks:** BASE-005 (H-0001), EDA-001, EDA-006, EXP-002

## Decisions

1. **H-0001: lookbacks count bars of the signal timeframe** — approved. The same fixed
   parameters apply to the 1d and the 1h signal bars (ADR 0035).
2. **H-0001: the fold-aligned copy of a rule baseline is not a trial** — approved. It restricts
   the same configuration's returns to the test folds; the trial budget stays 36 (ADR 0035).
3. **EDA records no trials** — approved. EDA runs evaluate no trading configuration (ADR 0036).
4. **A standing descriptive hypothesis H-0000**, with a zero trial budget, is the hypothesis EDA
   runs belong to. It is registered alongside H-0001 once real data fixes the windows. Its
   pre-registration needs the EXP-002 schema to accept a zero trial budget (it requires at least
   one today); that change comes with the registration.
5. **Per-session admission is report-only.** The admission list, and so `config/horizons.yaml`,
   admits horizons on their overall ratio only. The cost-to-volatility table still reports each
   session's ratios, with `below_bound` in place of `admitted` so that no row reads as an
   admission. A session-restricted horizon can be used only through a pre-registered hypothesis.

## Consequences

- The two H-0001 readings and "no trials for EDA" are no longer provisional (`docs/STATUS.md`).
  The board runner still has to implement the revised H-0001 before it runs (C-15).
- `xq research eda` keeps requiring `--hypothesis`; with real data it is run under H-0000.
- The research half of Sprint 5 (C-16) now includes H-0000's pre-registration and the schema
  change above.
