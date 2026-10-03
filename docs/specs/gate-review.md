# Gate review and sign-off (GATE-003)

This is the human review a strategy bundle needs before any promotion past a gate it has passed
on the registry's evidence. `xq gate evaluate <bundle>` writes a copy next to each gate report
(`reports/gates/<bundle>/<time>/review.md`) with the fields in braces filled in. The reviewer
completes it, signs it, and keeps it with the gate report. A promotion recorded with
`xq registry promote` names this review in its `--reason`.

The gates are fixed before results are seen (`config/gates.yaml`). A reviewer may refuse a bundle
that passed them; a reviewer may never pass a bundle that failed them, or change a threshold to
let it pass (CLAUDE.md). No LLM, including Claude, makes or signs this decision.

## Subject

- Bundle: `{bundle_id}` ({bundle_name}), status at evaluation: {bundle_status}
- Origin run and strategy: {origin}
- Gate report: {gate_report}
- Validation run: {validation_run}
- Gate results: {gate_results}
- Reproduction of the origin run (EXP-006): {reproduction}

## Evidence checklist

Tick each item after reading its evidence in the gate report; write what you checked.

| # | Item | Default criterion | Reviewed | Notes |
| --- | --- | --- | --- | --- |
| 1 | Data validation | No FAIL partitions included; WARN days reviewed | [ ] | |
| 2 | Baseline comparison | Beats the best baseline (R1) | [ ] | |
| 3 | Statistical validation | R2 thresholds (DSR, PBO, SPA) | [ ] | |
| 4 | Out-of-sample testing | R3, one vault run, access logged | [ ] | |
| 5 | Walk-forward testing | At least 60 % of folds positive; no significant decay | [ ] | |
| 6 | Transaction-cost testing | Positive at 1.5x spread and 2x slippage | [ ] | |
| 7 | Robustness testing | At least 70 % of the neighbourhood profitable; smooth delay decay | [ ] | |
| 8 | Monte Carlo testing | 95th-percentile drawdown below the halt level | [ ] | |
| 9 | Drawdown analysis | Worst drawdown and duration within the risk budget | [ ] | |
| 10 | Paper trading | R4 | [ ] | |

## Questions the reviewer answers

1. **Is any result too good?** A net Sharpe ratio above 3, or a hit rate far above 55 % on
   returns, is treated as a leakage bug until proven otherwise (CLAUDE.md). What was checked?
2. **Is the trial count honest?** Does the family's raw and effective trial count include every
   exploratory run on this hypothesis? Is the raw/effective ratio above the review ratio (10)?
3. **Which criteria were not applicable, and why?** PBO without a meaningful selection and a
   neighbourhood fixed a priori are the owner's only rules (ADR 0058). Is each declaration's
   source credible, and was it made before any result existed?
4. **Which criteria could not be evaluated?** A verdict with any of them is incomplete and does
   not pass.
5. **Which warnings did the evidence carry** (an over-rejecting SPA, a provisional cost model or
   risk profile), and do they change your confidence?
6. **Are the costs real?** Net figures marked "screening, placeholder costs" are not a broker's
   terms. Has the cost model been confirmed?
7. **Was the hypothesis registered before the evaluation data was seen**, and does the bundle
   trade exactly what it tested?
8. **Is the vault untouched by anything but this bundle's one evaluation?** (`vault_access_log`)

## Decision

- [ ] Approve the promotion to: ____________________ (the next status only)
- [ ] Reject: ____________________
- [ ] Request more evidence: ____________________

Reasons:

## Sign-off

| Role | Name | Date | Signature |
| --- | --- | --- | --- |
| Reviewer | | | |
| Project owner | | | |

A live status transition additionally needs the live-readiness checklist (GATE-004,
`docs/specs/live-readiness.md`), which is not built yet.
