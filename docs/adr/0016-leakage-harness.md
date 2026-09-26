# ADR 0016 — Leakage harness

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DS-006 (with DS-002 and DS-003, which it tests)

## Context

Phase 3 asks for a harness applied to every feature and target: (a) truncation invariance at
random t, (b) randomizing data after t leaves values at or before t unchanged, (c) each row's
inputs have `available_at <= decision_time`, (d) any feature with |corr| > 0.9 to a target at lag
0 fails pending review. Acceptance: it catches five planted leaks (centered rolling mean,
full-sample z-score, `bfill`, higher-timeframe join on bar start, target shifted into features).
CLAUDE.md requires every new feature and target to join the parametrized suite in
`tests/leakage`.

## Decision

1. **Feature contract.** A feature is a function of named input frames (bars, context bars), each
   with an availability column (`available_at_utc`), returning a frame indexed by unique,
   increasing, tz-aware decision times. Pure functions of inputs make the checks possible: the
   harness recomputes them on altered inputs.
2. **(a) Truncation** keeps only input rows with `available_at <= t` for about 25 decision times
   (always the first and last, the rest drawn with a seeded generator) and compares every output
   row at or before t. **(b) Perturbation** multiplies every numeric input value available after
   t by a random factor (integers shifted, booleans flipped; time columns untouched) and compares
   the same rows. Both use a relative tolerance of 1e-9, so floating-point noise cannot hide a
   leak and cannot fake one either: the causal primitives are bit-identical under truncation.
3. **(c) Availability audit** uses provenance. Every column whose name ends in `available_at`
   must be at or before its row's decision time. `asof_join` always writes the matched row's
   `available_at`, so every joined value is audited.
4. **(d) Correlation scan** compares each numeric feature with each target on common decision
   times (at least 30 finite pairs, non-constant series). |corr| > 0.9 fails; a reviewed pair
   passes only if it is listed explicitly in `allowed`, which keeps the review visible in code.
5. **Targets** look forward by definition, so they are checked against their declared bounds:
   removing quotes after `label_end`, perturbing quotes before the decision time and perturbing
   sigma-hat at every other decision time must each leave the value unchanged, and
   `decision_time <= label_start <= label_end`. This proves purging by `label_end` is safe and
   that the only information at or before t a target uses is sigma-hat at t.
6. **Reports, not booleans.** Each check reports the first instant and column where it failed,
   so a failing test points at the leak. A violation is a leak until proven otherwise.
7. **Suites.** `tests/leakage/test_planted_leaks.py` holds the five planted feature leaks (each
   must fail the expected checks) with their correct counterparts (each must pass), plus three
   planted target leaks (understated `label_end`, entry at the signal bar's close, sigma-hat from
   the future). `tests/leakage/test_primitives.py` runs every DS-003 primitive and the
   availability join through the harness; features and targets added later join the same way.
   The inputs are synthetic bars made by the real bar builder from dense synthetic ticks.

## Consequences

- The harness found a real bug before any feature was built on it: `asof_join` failed on an
  empty right frame (context bars truncated before the first one exists). It is fixed.
- The perturbation check depends on the random factor changing a value; a leak through a column
  that is constant (or zero) in the future rows could slip past (b), but not past (a).
- The correlation scan is a tripwire, not a proof: a leak with |corr| below 0.9 is caught only by
  (a) to (c).
