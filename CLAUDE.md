# CLAUDE.md — binding rules for this repository

This repository is the XAUUSD Quantitative Research Platform (Python package `xq`, CLI `xq`).
The goal is research whose results survive unseen data and realistic costs, not a
profitable-looking backtest.

## Sources of truth

1. `docs/specs/development-plan.md` — phases, backlog (task IDs), sprint order, gates.
2. `docs/specs/project-instructions.md` — research philosophy and requirements.
3. This file — binding invariants.
4. `docs/adr/` — decisions already made.
5. `CHANGELOG.md` and `git log` — what is already done.

The development plan wins on architecture and sequencing; the project instructions win on
research standards. Record any conflict in an ADR.

## Commands

```bash
uv sync                               # install the locked environment
uv run pytest                         # full test suite
uv run ruff check && uv run ruff format --check
uv run mypy                           # strict type checking
uv run xq --help                      # CLI
```

## Binding invariants (violating any of these is a critical bug)

- Timestamps are stored in UTC as int64 nanoseconds and handled as tz-aware values in pandas.
  Naive timestamps are rejected at every public boundary.
- Bars cover `[start, start + tf)`, are labelled by start and carry
  `available_at = start + tf + publication_latency`. Nothing is used before its `available_at`.
- Decision time t is the `available_at` of the latest base-timeframe bar. Features at t use only
  rows with `available_at <= t`.
- The trading day rolls at 17:00 America/New_York. Daily bars follow that roll. Sessions are
  defined in local time zones and converted to UTC per date.
- Every source adapter declares its clock convention. Broker server time is never assumed to be UTC.
- No look-ahead: no centered windows, no full-sample scaling or normalization, no `bfill` into the
  past, no joins on bar start across timeframes (use `asof_join` on `available_at`), no
  smoothed/Viterbi regime states, no target-derived features. Every new feature and target must pass
  the leakage harness (`tests/leakage`) — add it to the parametrized suite, never skip it.
- Raw data is immutable. Cleaning flags; it does not silently repair. Every action is logged.
- Fills happen at the next available quote on the correct side (buy at ask, sell at bid) plus
  modelled slippage, commission and financing. Never fill at the signal bar's close or at mid.
- Walk-forward only for financial time series. No shuffled splits. Purge by `label_end`, embargo.
  Hyperparameter search, early stopping, calibration and thresholds use training/validation data only.
- The vault (data after `vault.start`) is never loaded without a gate token. Never work around it.
- Every research run goes through the experiment run context and is counted by the trial counter.
  Confirmatory runs require a clean git tree.
- Thresholds, stops and targets are in volatility units or basis points, never fixed dollars.
- Models never size positions. Every order passes through `RiskEngine.evaluate`; `OrderIntent` can
  only be built from an approved `RiskDecision`.
- Gate thresholds in `config/gates.yaml` are fixed before results are seen. Never change them to
  make a candidate pass; raise it with the owner instead.
- An LLM (including Claude) never makes or overrides trading decisions at runtime.

## Engineering standards

- Python 3.12, uv, pandas/NumPy, pydantic config, structlog, pytest + hypothesis, ruff, mypy (strict).
- Configuration is a frozen `AppConfig` passed explicitly; no module-level mutable state.
  Parameters live in `config/`, not in code.
- Seed all randomness through `xq.core.seeds`; record seeds in run metadata.
- Add a dependency only in the sprint that needs it, with a one-line justification in the commit.
- Public functions have type hints and docstrings. Keep modules within the layout in section 3 of
  the plan; propose layout changes through an ADR.
- Tests must pass under a non-UTC local timezone (CI runs with `TZ=Asia/Tokyo`).

## Definition of done for a backlog task

Code implements the task as specified; its tests exist and pass together with the whole suite;
ruff, ruff format and mypy pass; `CHANGELOG.md` has an entry under "Unreleased" with the task ID;
docs and ADRs are updated; one logical Conventional Commit referencing the task ID, e.g.
`feat(data): DATA-008 bar builder with available_at`.

## Honesty rules

Never claim something works without running it. Never weaken, skip or xfail a test to get green
unless the test itself is wrong (and say why in the commit). Treat results that look too good
(net Sharpe above 3, hit rate far above 55% on returns) as a leakage bug until proven otherwise.
Distinguish "implemented", "tested" and "validated on real data".
