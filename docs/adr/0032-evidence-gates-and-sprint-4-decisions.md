# ADR 0032 — Evidence gates and owner decisions at the Sprint 4 hold point

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, answering the Sprint 4 hold point (the proposed `config/gates.yaml`
  and the open questions in `docs/STATUS.md`)
- **Tasks:** VAL-007; changes TGT-002 (ADR 0025, ADR 0026), BT-001 (ADR 0029), BT-002 (ADR 0030)
  and ARCH-007/ARCH-008 (the CI workflow and the Docker image)

## Context

The plan requires the evidence gates to be written down before any candidate is evaluated
(Phase 10 and Phase 17: "ratified before the first candidate is evaluated; changes need an ADR").
Sprint 4 stopped before VAL-007 until the owner approved them. The owner approved the proposal
with changes and answered four further questions at the same time.

## Decision 1 — The evidence policy (`config/gates.yaml`)

The owner's text is committed unchanged below a header comment. Its rationale, section by section:

**Conventions (how every gated statistic is computed).**

- `returns: daily_net` — gates see daily (trading-day, 17:00 New York roll) returns after every
  modelled cost, the unit of BT-002 and VAL-001. Per-bar returns would inflate the sample size
  and understate uncertainty.
- `annualization: backtest.periods_per_year` — one annualization for every report and gate, so a
  gate cannot disagree with the board. 252 is **provisional**: the configured calendar
  (`config/sessions.yaml`) has 257–259 open trading days a year in 2022–2025.
- `trial_count: effective` — deflation (DSR, SPA) uses the clustered effective number of trials
  (ADR 0023, ADR 0026: correlation 0.7, at least 60 common trading days). These clustering
  parameters are **frozen** with the gates; a test pins them. `report_raw_trial_count: true`
  keeps the raw count beside it, and when raw / effective exceeds `raw_vs_effective_review_ratio`
  (10) the result is flagged for owner review: that much clustering suggests near-duplicate trials
  or a clustering problem, and the effective count may then be too kind.
- `one_sided: true` — every gate asks "is it better?", never "is it different?".
- `bootstrap` — stationary bootstrap (VAL-001) with 10,000 resamples; the mean block length is
  chosen per series by Politis and White (2004, corrected by Patton, Politis and White 2009),
  and never shorter than 5 days, so a week of dependence is always kept even when the automatic
  rule sees none.

**R1 research candidate** (to the event backtest): the plan's R1 plus an explicit margin over the
best baseline (`best_baseline_margin_sharpe: 0.0`: the candidate's Sharpe must exceed the best
baseline's, and the paired test must reject at 0.10).

**R2 validated** (to one vault run): the plan's R2, plus four requirements added by the owner:

- `oos_max_drawdown_max: 0.15` — the realized out-of-sample drawdown must not exceed the 15 % halt
  level; the Monte Carlo quantile alone could pass a path that actually halted.
- `decay_trend` — fail if the slope of performance over time is significantly negative (one-sided,
  5 %): an edge that is fading is not validated by its average.
- `execution_delay` — net Sharpe must stay positive with execution one bar later, so the result
  does not hinge on the fastest possible fill.
- `min_track_record` — the out-of-sample length must reach the minimum track record length of
  VAL-001 at 95 % confidence.

**R3 vault pass** and **R4 paper pass** are the plan's defaults.

### Boundary rules (implementation, `GatesConfig.criteria`)

Every threshold becomes one `GateCriterion` (`value <op> threshold`; a missing value fails). Where
the plan states the comparison it is used as written; where it does not, `_min` means at least and
`_max` at most, except that a Sharpe floor must be exceeded (a Sharpe ratio of exactly 0 is no
edge). The rules below are fixed now, before any result, and are tested:

| Gate | Threshold | Passes when |
| --- | --- | --- |
| R1 | `oos_net_sharpe_min` 0 | Sharpe > 0 |
| R1 | `oos_sharpe_p_max` 0.05 | p < 0.05 |
| R1 | `best_baseline_p_max` 0.10 | p < 0.10 |
| R1 | `best_baseline_margin_sharpe` 0 | Sharpe − best baseline Sharpe > 0 |
| R1 | `min_oos_trades` 100 | closed trades ≥ 100 |
| R2 | `dsr_min` 0.95 | DSR ≥ 0.95 |
| R2 | `pbo_max` 0.20 | PBO ≤ 0.20 |
| R2 | `spa_p_max` 0.10 | p ≤ 0.10 |
| R2 | `stressed_costs.net_sharpe_min` 0 | Sharpe at 1.5× spread and 2× slippage > 0 |
| R2 | `parameter_neighbourhood.profitable_share_min` 0.70 | share of the ±20 % neighbourhood profitable ≥ 0.70 |
| R2 | `positive_folds_share_min` 0.60 | share of positive folds ≥ 0.60 |
| R2 | `max_single_year_pnl_share` 0.50 | largest single-year share of P&L ≤ 0.50 |
| R2 | `monte_carlo_drawdown.below` 0.15 | 95 % quantile of Monte Carlo drawdown < 0.15 |
| R2 | `oos_max_drawdown_max` 0.15 | OOS maximum drawdown ≤ 0.15 |
| R2 | `decay_trend.significance` 0.05 | one-sided p of a negative slope ≥ 0.05 |
| R2 | `execution_delay.net_sharpe_min` 0 | Sharpe with a one-bar delay > 0 |
| R2 | `min_track_record.confidence` 0.95 | OOS days / MinTRL at 95 % ≥ 1 |
| R3 | `net_sharpe_min` 0 | vault Sharpe > 0 |
| R3 | `walk_forward_interval` 0.90 | 0.05 ≤ quantile of the vault Sharpe in the walk-forward bootstrap distribution ≤ 0.95 |
| R3 | `risk_limit_breaches_max` 0 | breaches ≤ 0 |
| R3 | `vault_access_logged` true | logged |
| R4 | `min_months` 3, `min_trades` 100 | ≥ |
| R4 | `realized_slippage_ratio_max` 1.5 | realized / modelled slippage ≤ 1.5 |
| R4 | `monte_carlo_percentile_min` 0.10 | percentile in the Monte Carlo band > 0.10 |
| R4 | `shadow_parity_min` 1.0 | parity ≥ 1.0 (100 %) |
| R4 | `unresolved_incidents_max` 0 | incidents ≤ 0 |

The R3 interval is two-sided on purpose: a vault result far *above* the walk-forward interval is
as suspicious as one below it (CLAUDE.md: results that look too good are treated as leakage).

### Loading

- `config/gates.yaml` is a configuration fragment (`AppConfig.gates`, `cfg.gates_config()`),
  validated strictly: unknown keys, probabilities outside (0, 1), negative Sharpe floors, cost
  multipliers below 1, `one_sided: false` and any other `returns` convention are refused, because
  the code implements only what the approved text says.
- **Only that file may set `gates`.** A `gates:` key in `base.yaml` or a profile, an
  `XQ_GATES__*` environment variable or a `--set gates.…` override is refused with a
  `ConfigError`, so no layer can move a threshold after results are seen.
- `annualization: backtest.periods_per_year` must resolve (`cfg.gate_periods_per_year()`), and an
  effective trial count requires the trial-clustering settings.
- `gates_hash` fingerprints the policy; results evaluated against it record the hash.
- VAL-001 gains `politis_white_block_length` (matches `arch` 8.0.0 on five reference series to
  1e-9 and recovers the AR(1) optimum), `gate_block_length`, and `bootstrap_sharpe` — the
  percentile interval and the one-sided p-value `(1 + #{SR*_b − SR ≥ SR}) / (1 + B)`, which
  imposes the null by centring the bootstrap distribution on the estimate. Its size on
  zero-mean AR(1) returns is close to nominal (tested).

The gate evaluator itself is GATE-001 (Sprint 13); the paired baseline test, PBO, SPA, the Monte
Carlo and robustness measures arrive with their tasks.

## Decision 2 — Horizon lengths in trading time

`1d` means exactly one trading day: **23 market hours** (the 18:00–17:00 New York session), not
24 (which ended one hour into the next session, ADR 0026). `4h` is 4 market hours; minute and hour
horizons are unchanged. A day count is converted with the regular trading-day length of the
configured market hours (close minus open, 23 h), so an early-close day does not shorten `1d`:
the horizon is a fixed amount of market time. Vol-normalized targets scale by
`sqrt(1380 minutes)` for `1d`. The `forward_return` kind moves to code version 3.

## Decision 3 — Decisions taken while the market is closed

A decision whose time is not inside a market-open interval (for example the 17:00 close itself,
where the last bar of the day becomes available, or any time in the daily break or a weekend)
gets **no target label** and **no entry**. This replaces "entered at the reopen" (ADR 0026 §4,
ADR 0030 §2). A decision taken while the market is open keeps its label when its holding period
spans a close, with `crosses_close = true`.

- Targets: such decision rows keep their features but have a missing value, `label_start` and
  `label_end` (so they are never trained on and never purge anything). `forward_return` moves to
  code version 4.
- Screener (BT-002): such a decision places no order at all — no entry, no exit, no change of
  exposure. The position actually held stays until the next decision taken while the market is
  open, and the skipped decisions are reported in `BacktestResult.closed`.

## Decision 4 — Costs stay provisional; financing is a cost on both sides

The placeholder cost model stays (ADR 0029). Until broker terms replace it, **financing is a cost
on longs and on shorts**: a cost model marked `provisional: true` must have strictly positive long
and short financing rates (a credit on either side is refused). A non-provisional model built from
real broker terms may have a negative (credit) rate.

Every net result produced with a provisional cost model is marked **"screening, placeholder
costs"**: the cost model exposes the label, `BacktestResult` carries it, and every report of net
results (the baseline board first) prints it in its header and on every row.

## Decision 5 — Verifying the Docker image

The image cannot be built in the Claude sandbox (C-6). The Dockerfile is split into a `base`
stage (the locked runtime dependencies and the package, as before), a `test` stage (base plus the
dev dependencies, `git`, `tzdata` and the test suite) and the `runtime` stage, which stays last so
it remains the default build and the image `docker compose` builds (now also named as its
`target`). CI gets a `docker` job that builds and starts the runtime image, builds the test stage
and runs the suite inside it as the non-root user under `TZ=Asia/Tokyo`, like the main job.

## Consequences

- VAL-007 is done: the gates exist before any candidate result. Changing a threshold needs the
  owner's approval and a new ADR; the pinned-values test fails otherwise.
- Dataset ids change twice through the `forward_return` code version (no real dataset exists).
- Labels now end on the same session clock time a trading day later for `1d`; the leakage suite
  and label-window assertions use the new market-time horizon.
- Decisions at 17:00 lose their labels; a 15-minute dataset loses one labelled row per trading day.
- Buy-and-hold shorts and short-biased strategies pay financing in every screening result.
- A green CI now also means the image builds and the suite passes inside it.
