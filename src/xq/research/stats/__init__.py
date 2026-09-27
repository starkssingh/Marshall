"""Statistical time-series research (Phase 5: STAT-001, STAT-002, STAT-003, STAT-006, STAT-008).

Formal tests of stationarity, dependence and linear predictability. Every test returns a typed
`StatResult` (statistic, p-value, lags, null and alternative, assumptions, decision) and every
method passes a recovery test on a simulated process with a known answer before anything uses it
(`xq.research.recovery`). The verdict report (`xq.research.stats.verdict`) turns results into the
Observed / Evidence / Interpretation / Limitations / Action format. Sprint 6 is build-only: none of
this has run on real data (ADR 0043).
"""
