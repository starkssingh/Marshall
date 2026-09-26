# ADR 0012 — Quality runs, report and vault handling in `xq validate`

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DQ-006 (refines ADR 0010 §5 for quality checks)

## Context

DQ-006 asks for a report (summary table, per-check statistics, top anomalies with timestamps,
missing-minutes heatmap week × hour, spread heatmap hour-of-week) stored in the database and under
`reports/quality/<run_id>/`. The runner must also decide what inputs each check sees and how the
vault is treated. ADR 0010 said quality checks may process vault partitions because the release
gate needs validated vault data.

## Decision

1. **Unit of work.** One partition per trading day of a source. For each day the runner reads the
   clean partition of the configured rules version and the 1-minute bars of the configured build,
   and adds the session-table row, the latest spread statistics (DATA-009; if missing, the spread
   check does not apply), the tick-rate norm, the per-rule counts of ticks cleaning dropped (from
   `cleaning_actions`) and the source's data coverage (from `raw_files`).
2. **Tick-rate norm.** The median tick count per New York hour of week across the validated
   window, only for hours observed in at least `min_weeks` (4) weeks; with less history the check
   does not apply rather than comparing a day with itself.
3. **Vault.** `xq validate` covers only trading days that end at or before `vault.start` unless
   `--include-vault` is given. That flag exists for the release-gate procedure (GATE-001/002),
   is recorded as `quality_runs.includes_vault`, and should not be used during research: the
   report's anomalies show prices and times, so a vault report is a look at the holdout.
4. **Persistence.** Migration 0002 adds `quality_runs` (window, rules and build versions,
   `includes_vault`, git sha, config hash, report path, summary JSON) and `quality_results` (one
   row per run, partition and check: severity, metric, thresholds, status, details with the kept
   anomalies). DQ-007 (Sprint 3) gates datasets on these rows.
5. **Report format.** Markdown (`report.md`) plus `summary.json` and two PNG heatmaps
   (matplotlib, Agg backend). Markdown renders on GitHub and diffs well, so the plan's
   "HTML/Markdown" is satisfied with Markdown only. Checks that did not apply show 0 days
   evaluated.
6. **Exit status.** `xq validate` exits 0 whenever the run completes, even with FAIL results:
   grading is data, and gating is DQ-007's job.

## Consequences

- A quality run is fully reproducible from its recorded versions, config hash and git sha.
- Until DQ-008 reviews a report built from at least one year of real broker ticks, the proposed
  thresholds in ADR 0011 remain provisional and Sprint 2 is not complete.
