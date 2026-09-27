# ADR 0045 — Statistical baselines on the boards (BASE-003)

- **Status:** accepted (the H-0001 question is open for the owner)
- **Date:** 2026-09-27
- **Tasks:** BASE-003 (on STAT-006 and VOL-003)

## Context

BASE-003 puts the statistical baselines of STAT-006 (AR) and VOL-003 (EWMA, HAR) on the boards.
The baseline board of H-0001 (`experiments/configs/baselines/board.yaml`) is part of a draft the
owner approved with a trial budget of 36 (ADR 0035, ADR 0041); every forecast baseline except
`zero_return` becomes a forecast-sign strategy there, and every strategy is a trial.

## Decisions

1. **`ar1` is a forecast baseline** (`xq.models.baselines`): an AR(1) of the decision bars' own
   log returns fitted on each training fold (`xq.models.arma`, the model of STAT-006), iterated to
   the target's horizon in base bars. Its order is part of its definition: a configuration cannot
   change it (baselines are benchmarks, never tuned). The board runs it like the other forecast
   baselines — forecast metrics, the Diebold-Mariano test against `zero_return`, and a
   forecast-sign strategy — wherever a board configuration lists it; an end-to-end test runs it on
   a synthetic dataset.
2. **The ARMA model lives in `xq.models.arma`** so the board can use it; `xq.research.stats.arima`
   keeps the STAT-006 study and re-exports the model.
3. **EWMA and HAR are the volatility board's benchmark entries** (`board_forecasters`): the default
   of the sigma-hat selection (`ewma_0.94`) and the reference of its Diebold-Mariano tests (`har`),
   evaluated on the same folds as every other volatility model (ADR 0044).
4. **H-0001's board is not changed.** Adding `ar1` to `board.yaml` would add four forecast-sign
   strategies and raise H-0001's approved trial budget from 36 to 40. That is the owner's
   decision; until it is made, `ar1` is available to boards but not in H-0001's.

## Consequences

- Open owner decision: include `ar1` in H-0001 (budget 40) or evaluate it under its own
  pre-registered hypothesis.
