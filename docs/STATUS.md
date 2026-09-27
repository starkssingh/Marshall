# Project status

Read this after `CLAUDE.md` at the start of every session. It is updated at the end of every
sprint and whenever a decision or carry-over item changes; anything decided in conversation is
recorded in an ADR and here in the same session. If a memory of an earlier conversation conflicts
with the repository, the repository wins.

- **Last updated:** 2026-09-27, after Sprint 9 was merged (PR #14; C-22 decided and implemented
  at its start, ADR 0053)
- **Merged to `main`:** Sprints 1–6, 9, 11 and 12 A with the revised H-0001 draft and the Sprint 5
  and Sprint 6 review fixes (PRs #2, #3, #6, #7, #8, #9, #10, #11, #12, #13, #14).

## Current sprint

- **Sprint:** 9 — statistical validation and robustness (ADR 0051, ADR 0054) — **complete, merged
  in PR #14, synthetic data only**; its open points (C-24) wait for the owner's review. First the owner's Sprint 12 A decision C-22: sizing on the edge
  per unit of risk (`5ab2678`, ADR 0053). Then, one commit each: VAL-003 PBO by CSCV (`b516a07`),
  VAL-004 Reality Check, SPA and Romano–Wolf (`1dc1fbc`), VAL-006 Holm and Benjamini–Hochberg per
  family (`2a95486`), ROB-001 parameter perturbation (`6403286`), ROB-002 cost and latency stress
  (`87457ae`), ROB-003 block bootstrap and trade permutation (`2dd5412`), ROB-006 slicing read
  from the pre-registered hypothesis (`d8db5d9`), ROB-007 execution delay (`3d0c4fb`) and EXP-006
  `xq exp reproduce` (`ffe51a1`). Every method is proven on simulated strategies with known truth
  (noise-only families, a single-point optimum on noise, a genuine trend edge, planted edges) and
  listed in the recovery registry; its open points are C-24. **Nothing has run on real data.** The
  previous sprint, 12 A — risk and signal engines, data-independent part — is **complete, merged
  in PR #13, synthetic data only**. In Sprint 12 A, first the Sprint 11 review decisions
  (ADR 0050): the
  reconciliation tolerance after the sizing effect (`c4c490b`), limit orders filling only on a
  trade through by at least one tick (`d835141`) and the TGT-002 closed-market fill fix
  (`fdd5261`, forward-return code version 5). Then RISK-001 … RISK-006 and SIGNAL-001 …
  SIGNAL-005 (ADR 0052), one commit each. The Sprint 11 placeholder risk approver is **removed
  and replaced by the real `RiskEngine` everywhere**; the regime filter is an interface with only
  a clearly marked PLACEHOLDER pass-through, since no regime model exists (REG-007, Sprint 8).
  The previous sprint, 11 — event-driven backtester — was merged in PR #12. **Nothing has run on
  real data.**
- **Working system:** library code, exercised by the tests. The event backtester
  (`run_event_backtest`, tick or bar mode) now runs the whole chain the plan prescribes:
  forecasts → `SignalEngine` (a strategy defined entirely by YAML; uncalibrated, stale and
  filtered forecasts refused; EV in sigma units with a conservative variant; a `SignalRecord` for
  every candidate) → `TradeIntent` → session constraints → `RiskEngine.evaluate` (pure; risk
  state rebuilt from the ledger; fixed-fractional or volatility-target sizing with the
  calibrated-probability and drawdown scaling; stop policy; halts at their exact thresholds;
  caps; kill switch and data-health breakers; exits always approved) → `OrderIntent` (only from an
  approved decision the risk engine issued — enforced at runtime and by an AST architectural
  test) → `SimulatedBroker` → `Portfolio` → `Ledger`; `SignalStrategy.audit` traces every fill
  back to its forecasts. Stops everywhere are in sigma-hat units (a supplied daily sigma-hat or
  the interim EWMA of signal bars). The decision-chain schemas are exported as JSON Schemas to
  `docs/specs/interfaces/`. There is no CLI for event backtests yet: they wait for real data and a
  candidate. Sprint 9 adds the validation and robustness library: `xq.validation.pbo`,
  `xq.validation.spa`, `xq.validation.multiple_testing`, and `xq.robustness.perturb`,
  `costs_stress`, `bootstrap`, `slicing` and `delay`. Each gives its R2 check from
  `config/gates.yaml` through `GatesConfig.criterion` where a gate applies. `xq exp reproduce
  <run_id>` rebuilds a run's dataset, reruns it (baseline-board runs) and compares every metric
  within tolerance. **Not built:** `xq validate-strategy <run_id>`, which the plan's Sprint 9
  "working system" names but no Sprint 9 task covers. The robustness report and score are ROB-008,
  and the full statistical report goes with GATE-001 (C-24).
- **Next:** the owner's review of Sprint 9 (C-24), then the owner chooses the next sprint. Sprint
  9's ROB tasks unblock ROB-004, ROB-005 and ROB-008 (Sprint 12's remaining part, with
  `xq validate-strategy` if the owner wants it there); the real regime filter waits for REG-007
  (Sprint 8). Still open from earlier sprints: Claude
  implements the revised H-0001 in the board runner (C-15); with real data (C-8): the research
  halves of Sprint 5 (C-16) and Sprint 6 (C-18); then Sprints 7, 8 and 10.
- **Not allowed yet:** running H-0001 (or any board) on real data before the owner has approved
  and registered it; generating an EDA report, a statistical verdict report or a volatility board
  on real or pseudo-real data, or writing values to `config/horizons.yaml`, before the owner
  allows it (ADR 0035, Sprint 6 instruction); promoting a volatility forecaster or replacing the
  interim sigma-hat (ADR 0044); registering H-0000 before real data fixes its windows (ADR 0041);
  running event backtests, reconciliations or reports on real or pseudo-real data (ADR 0048);
  treating any event-tier result as evidence (synthetic quotes, placeholder costs, a provisional
  risk profile, no regime model); treating the template strategy or the synthetic test
  forecaster as a candidate; running the Sprint 9 validation or robustness methods on real or
  pseudo-real results, or citing their output as evidence, before a candidate exists and the
  owner allows it; starting the sprints after Sprint 9 before the owner's review of Sprint 9;
  pushing to `main`.
- **Earlier sprint (6, merged in PR #11):** statistical and volatility research, build-only —
  STAT-001, STAT-002, STAT-003, STAT-006, STAT-008 (framework), VOL-001 … VOL-006 and BASE-003,
  each passing a recovery test on simulated processes (`xq.research.recovery.RECOVERY_TESTS`,
  ADR 0043); Holm across the VOL-006 challengers and separate trial families for forecasting
  models (ADR 0046). No statistical report or volatility board has run on real data, and no
  volatility model is promoted: sigma-hat stays the interim EWMA of `fwd_returns.v1`.

## Carry-over items from reviews

| ID | Item | From | Owner | Closed by |
| --- | --- | --- | --- | --- |
| C-1 | Record the Sprint 3 review decisions in one ADR | Sprint 3 review | Claude | `4c67cfe` (ADR 0026) |
| C-2 | Fill-delay diagnostic: count fills delayed > 5 s in target-build output | Sprint 3 review | Claude | `5ebfa87` |
| C-3 | Rollover window 16:45–18:15 America/New_York (US release window unchanged) | Sprint 3 review | Claude | `3d141a1` |
| C-4 | Trial clustering: keep ρ 0.7, require 60 common daily points | Sprint 3 review | Claude | `f15ef30` |
| C-5 | Trading-time horizons (market-open minutes only) and a `crosses_close` target column; leakage tests and `label_end` checks updated | Sprint 3 review | Claude | `523821d` |
| C-6 | Rebuild the Docker image and run the suite inside it (`scipy` added unchecked) | Sprint 3 review | Claude | `0b84ca7`: CI `docker` job (owner's choice, ADR 0032); first run 36290821094 built and started the runtime image and passed 796 tests inside the test stage as the non-root user under `TZ=Asia/Tokyo` |
| C-7 | `resample_causal` must respect availability (latency argument, test with latency > 0) | Sprint 3 review | Claude | `157a78c` |
| C-8 | Run `xq validate` on ≥ 1 year of real broker ticks, then the DQ-008 human review | Sprint 2 | Owner (data), then Claude | open — blocked on real data |
| C-9 | Commit the approved `config/gates.yaml` (VAL-007) with its rationale in an ADR | Sprint 4 hold point | Claude | `72a8cdc` (ADR 0032) |
| C-10 | `1d` = one trading day (23 market hours); `4h` = 4 market hours | Sprint 4 hold point | Claude | `34dd006` |
| C-11 | No label and no entry for decisions taken while the market is closed; open decisions crossing a close keep their label (`crosses_close`) | Sprint 4 hold point | Claude | `d9aa756` |
| C-12 | Financing a cost on long and short while costs are placeholders; every net result marked "screening, placeholder costs" | Sprint 4 hold point | Claude | `71711e3` (cost model), `4a71cae` (the board prints it) |
| C-13 | CI job that builds the image's test stage and runs the suite in it | Sprint 4 hold point | Claude | `0b84ca7` |
| C-14 | Review the draft `experiments/hypotheses/H-0001.yaml` (the baseline board), then register it before any real-data run | Sprint 4 | Owner | reviewed at the start of Sprint 5: revision requested (ADR 0035), continued as C-15 |
| C-15 | H-0001 draft revised as the owner asked (ADR 0035), still **unregistered**: rule baselines over the full pre-vault history after each rule's warm-up, with the fold-aligned version stored for comparison; rules on 1d and 1h signal bars (not 15m); trial budget 36 (24 rule + 12 forecast-sign strategies); discovery and evaluation windows "set from the real data's depth at registration" (registration is refused until they are); descriptive slices by year and by session (reported, not tested). Owner: review the revision, including two readings of Claude's (lookbacks count bars of the signal timeframe; the fold-aligned version is not a separate trial) — both **approved** at the Sprint 5 review (ADR 0041). Remaining: Claude implements the revision in the board runner; H-0001 is registered with windows from the real data, alongside H-0000 | Sprint 5 start | Claude (board runner), then owner (registration with real windows) | open — readings approved |
| C-16 | Research half of Sprint 5, after real data (C-8): fix `eda.discovery.end` from the data's depth; pre-register the standing descriptive hypothesis H-0000 (zero trial budget, family `descriptive`; the EXP-002 schema accepts it since `89f241a`, ADR 0042) alongside H-0001, and run EDA under it (ADR 0041); run the EDA confirmatory; review it; write `config/horizons.yaml` with `xq research admit-horizons`; write `docs/research/hypotheses-backlog.md` and pre-register its top items | Sprint 5 (build-only) | Owner (data, window, approval), then Claude | open — schema prerequisite done; blocked on C-8 and the owner's go-ahead |
| C-17 | DATA-013 secondary long-history adapter: build only if the owner decides a secondary feed is needed (depends on the broker's history depth) | Sprint 5 start | Owner (decision) | open |
| C-18 | Research half of Sprint 6, after real data (C-8) and the owner's go-ahead: pre-register the statistical and volatility studies (families and trial budgets); run STAT-001/002/003 on the discovery window and STAT-006 in walk-forward at the admitted horizons (needs `config/horizons.yaml`, C-16) and write the verdict report; run the volatility board on real 1m/5m bars (daily and hourly periods) on identical folds; apply `select_forecaster`; the owner decides whether the selected forecaster replaces the interim sigma-hat (an ADR and a configuration change); add a CLI for these reports; measure their speed on real data. Trial rules approved (ADR 0046): STAT-001 … STAT-003 record none (descriptive, under H-0000); STAT-006 and the volatility board record one per (model, horizon) in the `linear_forecasts` and `volatility_models` families. If STAT-002 or STAT-003 finds dependence in returns, write and pre-register H-0002 (linear predictability, with `ar1`) | Sprint 6 (build-only) | Owner (data, go-ahead, promotion), then Claude | open — blocked on C-8 and C-16 |
| C-19 | Whether the `ar1` forecast baseline (BASE-003) joins H-0001's board, which raises its approved trial budget from 36 to 40, or is evaluated under its own pre-registered hypothesis | Sprint 6 (ADR 0045) | Owner (decision) | decided (ADR 0046): `ar1` stays off H-0001 (budget 36), stays on benchmark boards, and gets H-0002 only if STAT-002/003 find dependence on real data (C-18) |
| C-20 | Owner review of Sprint 11's open points (ADR 0049): the weekly-close blackout length (60 min) and weekend-exit lead (30 min), provisional; the reconciliation tolerance applied to the raw equity difference, sizing included (at 100,000 USD, lot-step rounding alone can exceed 5 % of costs; reported separately); limit orders never filling better than their price; the provisional margin rate 0.05; Sprint 12's scope under ADR 0048 | Sprint 11 | Owner, then Claude | decided (ADR 0050, ADR 0051): tolerance after the sizing effect; 60-min blackout, optional weekend exit (off) and 5 % margin approved; limit orders fill only on a trade through by ≥ 1 tick, never better; Sprint 12's data-independent part now, then Sprint 9. Implemented: `c4c490b` (tolerance after sizing), `d835141` (limit trade-through) |
| C-21 | TGT-002 forward-return labels take the first quote at or after the intended fill time even when the market is closed (a stray quote in the daily break within the fill delay), the rule the screener no longer follows (`aa88b2a`). Fixing it bumps the target code version and changes dataset hashes, so it waits for the owner's go-ahead | Sprint 11 | Owner (go-ahead), then Claude | `fdd5261` (ADR 0050): market-hours fills only, forward-return code version 5 |
| C-22 | Owner review of Sprint 12 A's open points (ADR 0052): the provisional risk profile `risk-1` (everything but the owner's 0.5 % per trade and 15 % drawdown halt); refused intents that still close an opposite position (`risk rule:` exits); stops required on every long or short intent, widened when closer than 3 spreads and refused beyond 5 daily sigma-hats; the kill switch ignored by backtests unless given; **probability scaling on the raw calibrated p (0.5 → 0.6) suits 1:1 payoffs only — for 2:1 barriers break-even is p = 1/3, so either each strategy's profile matches its payoff or scaling moves to the edge p − SL/(TP + SL)**; the spread filter's hour-of-week median in New York time with an overall-median fallback | Sprint 12 A | Owner, then Claude | decided (ADR 0053): sizing scales on the edge per unit of risk, `clip(ev_r / ev_r_full, 0, 1)` with `ev_r = p_lcb x TP/SL - (1 - p_lcb) - cost/SL` and p_lcb the lower confidence bound; the other points approved as they stand (provisional profile values; a refused reversal still closes the opposite position; backtests may run without a kill switch). Implemented: the C-22 commit of Sprint 9 |
| C-23 | PAPER-001 requirement: the paper and live runtimes refuse to start without a kill-switch source (a file, an environment variable or the database flag); the backtester may run without one | Sprint 12 A review (ADR 0053) | Claude, when PAPER-001 is built | open |
| C-24 | Owner review of Sprint 9's open points (ADR 0054). (1) SPA and the Reality Check over-reject under strong serial dependence in short samples (AR(1) φ = 0.4, 400 periods: 15 % and 20 % at a 10 % level); test such families on non-overlapping periods. (2) The ±20 % neighbourhood gate reads the joint neighbourhood (3^k − 1 points), stricter than one parameter at a time. (3) Cost stress runs the plan's grid one dimension at a time plus the gate's joint scenario (no full factorial); a financing credit is divided by the multiplier. (4) Robustness drawdowns start from the capital; BT-003's `drawdown_metrics` does not (known issue, fix proposed as a separate task). (5) Volatility terciles are cut on the whole sliced period (descriptive only); slice names are checked when loaded, not at registration; the template's `volatility regime` became `volatility tercile`. (6) The execution delay shifts entries and exits alike. (7) `xq exp reproduce`: a reproduction adds no trials, the DSR is shown but not judged, provenance differences are reported rather than refused, and only `baseline_board` runs have a reproducer. (8) `xq validate-strategy` is not built: no Sprint 9 task covers it; proposed with ROB-008 and GATE-001 | Sprint 9 | Owner, then Claude | open |

## Open owner decisions

| Question | Options | Default in use |
| --- | --- | --- |
| Execution broker, venue and primary feed | MT5 broker (to be named), OANDA, cTrader | `mt5_primary` placeholder (ADR 0004): MT5 tick export, server clock `NY+7` |
| Secondary long-history feed if broker history is short (DATA-013, C-17) | Dukascopy, none | none; DATA-013 deferred until the owner decides (ADR 0035) |
| Discovery window (EDA-001) | the first 50–60 % of non-vault data (plan); a fixed end date | first 50 % of the span from the dataset's start to `vault.start`, at a trading-day start; to be fixed as `eda.discovery.end` from the real data's depth (C-16) |
| Broker cost terms (commission, financing rates, triple day, holiday financing) | broker's published terms | placeholder cost model (ADR 0029), financing a cost on both sides (ADR 0032) |
| Annualization of daily statistics (gates: "252, provisional") | 252; the calendar's open trading days (257–259 a year in 2022–2025) | 252 (`backtest.periods_per_year`) |
| Boundary rule of each gate threshold (`>` vs `>=`) | as tabled in ADR 0032 (plan wording where it states one; otherwise `_min` at least, `_max` at most, Sharpe floors strict) | ADR 0032 table |
| Account currency | USD, other | USD (plan default) |
| Research horizon focus | 15m–1d, other | 15m–1d; four horizons kept until EDA-006 (ADR 0026) |
| Risk budget | per-trade risk, drawdown halt | 0.5 % per trade, halt at 15 % drawdown (plan default; the gates' 0.15 drawdown limits match it) |
| Vault | holdout start | `2025-09-25T21:00:00Z`, the last 12 months at project start (fixed) |
| Replacing the interim sigma-hat (C-18) | the forecaster `select_forecaster` picks on real data; keep the interim EWMA | interim EWMA, span 96 base bars (`fwd_returns.v1`); nothing promoted (ADR 0044) |

Decided at the Sprint 4 hold point (ADR 0032): the evidence gates, the meaning of `1d`, decisions
taken while the market is closed, financing on both sides, and verifying the Docker image in CI.
Decided at the start of Sprint 5 (ADR 0035): the H-0001 revision (kept unregistered), a
build-only Sprint 5 with no EDA report on real or pseudo-real data and no `config/horizons.yaml`
values, and DATA-013 deferred. Decided at the Sprint 5 review (ADR 0040, ADR 0041): the six
EDA-006 changes; the H-0001 readings (lookbacks in signal-timeframe bars; the fold-aligned copy is
not a trial) and "EDA records no trials" approved; EDA runs belong to a standing descriptive
hypothesis H-0000 with a zero trial budget, registered alongside H-0001 once real data fixes the
windows; per-session admission is report-only, usable only through a pre-registered hypothesis.
Decided at the start of Sprint 6 (ADR 0042, ADR 0043, ADR 0044): a zero trial budget only for
family `descriptive`, with H-0000 not registered yet; Sprint 6 build-only — every method passes a
recovery test on a simulated process before use, the diurnal factor is fitted on training folds
only, the VOL-006 selection defaults to EWMA when nothing beats it, no reports on real data and no
model promoted. Decided at the Sprint 6 review (ADR 0046): Holm across the challengers in the
VOL-006 selection; forecasting-model trials in their own families (`linear_forecasts`,
`volatility_models`), never a trading-strategy family, and the trial rules approved with them;
`ar1` stays off H-0001 (budget 36) and gets its own hypothesis H-0002 (linear predictability) only
if STAT-002 or STAT-003 finds dependence on real data. Decided at the start of Sprint 11 (ADR 0047,
ADR 0048): no hypothesis may be registered in a reserved model family; while real data is pending
the data-independent engineering sprints run next — Sprint 11 on synthetic data only, with a
pass-through placeholder risk approver until Sprint 12, then Sprint 12 after the owner's review.
Decided at the Sprint 11 review (ADR 0050, ADR 0051): the reconciliation tolerance applies after
the separately reported sizing effect; the 60-minute pre-weekly-close blackout, the optional
weekend exit (off) and 5 % margin are approved provisional defaults; limit orders fill only when
the price trades through the limit by at least one tick, never better; TGT-002 closed-market
fills are fixed now; Sprint 12's data-independent part runs next (the regime filter an interface
with a pass-through), then Sprint 9; ROB-004, ROB-005, ROB-008 and the real regime filter wait for
their dependencies. Decided at the Sprint 12 A review (ADR 0053): position sizes scale on the edge
per unit of risk of a calibrated probability's lower confidence bound, net of costs (replacing the
raw-probability scaling); ADR 0052's other open points approved as they stand; the paper and live
runtimes must refuse to start without a kill-switch source (C-23, PAPER-001). Sprint 9's own
choices, made by Claude within the plan and the owner's instructions, are in ADR 0054 and wait
for the owner's review (C-24).

## Provisional assumptions not yet confirmed

| Assumption | Value in use | Where | Confirmed by |
| --- | --- | --- | --- |
| Broker | unnamed | `config/base.yaml` `sources.mt5_primary` | owner naming the broker |
| Source clock | `NY+7` (UTC+2/+3, US DST dates) | ADR 0003, ADR 0004 | broker documentation, DQ-004 on real data |
| Contract terms | tick 0.01, 100 oz per lot, lot step 0.01, max 100 | `config/instruments/xauusd.yaml` | broker contract spec |
| Trading calendar | 18:00–17:00 New York, NYSE holidays, 13:30 early closes | ADR 0002, `config/sessions.yaml` | broker schedule |
| Costs (commission, slippage, financing) | placeholder model, PROVISIONAL: commission 3.5 USD/lot/side; slippage 0.5 bp + 0.1·σ̂₁ₘ, ×3 rollover window, ×2 US release; financing 6 %/yr long, 2 %/yr short (both a cost, required while provisional), act/360, triple Wednesday; spread fallback p90 | `config/costs/placeholder.yaml`, ADR 0029, ADR 0032 | broker terms, paper trading |
| Execution latency | 1 s (market time from ADR 0026) | `config/targets.yaml` `fwd_returns.v1`, cost model | BT-001, paper trading |
| Event-tier execution rules | margin 5 % of notional (1:20); limit orders fill at their price, never better, and only when the price trades through them by at least one tick; bar mode (no ticks) resolves a bar touching both bracket legs to the stop; entry blackouts: rollover window, US release window, last 60 min before a weekly close; optional weekend exit 30 min before it (off) | `config/base.yaml` `backtest.event`, ADR 0049, ADR 0050 (defaults approved by the owner) | broker terms, paper trading |
| Risk profile of the event tier | `RiskEngine` with `config/risk/default.yaml` (`risk-2`, PROVISIONAL, values approved as provisional in ADR 0053): 0.5 % of equity to the stop and the 15 % drawdown halt are the owner's plan defaults; edge-per-unit-risk scaling with `ev_r_full` 0.25 and `lcb_z` 1.645; throttle 5 %→15 %, 20 lots, 3 × equity notional, 50 % margin use, 3 % daily loss, 4 h cooldown after 5 losses, 12 entries a day, stops within 3 spreads and 5 daily sigma-hats; breakers: quote older than 120 s, spread above 5 × the median of the last 500 quotes; kill switch `XQ_KILL_SWITCH`, no flattening | `config/risk/default.yaml`, ADR 0052, ADR 0053 | paper trading |
| Sigma-hat in the event tier | a supplied daily series, else the interim EWMA of signal-bar log returns (span 96, known after 20 returns, scaled by the square root of the signal bars in a 23-hour day); strategies' stops at `stop_sigmas` (3) of it | ADR 0052 | a VOL-006 selection on real data (C-18) |
| Regime filter | PLACEHOLDER pass-through: accepts a `RegimeState`, blocks nothing, says so in every signal record | `xq.signals.filters`, ADR 0051, ADR 0052 | REG-007 (Sprint 8) |
| Maximum fill delay | 300 s | `fwd_returns.v1`, cost model | ADR 0026: kept, provisional |
| Bar publication latency | 0 ms | `config/base.yaml` `bars` | live feed measurement |
| Quality thresholds | ratified provisional; one change allowed after DQ-008 | `config/quality.yaml`, ADR 0013 | DQ-008 review |
| Event windows | US release −5/+30 min; rollover 16:45–18:15 New York (ADR 0026) | `config/sessions.yaml` | EDA |
| Trial clustering | ρ 0.7, 60 common trading days; frozen with the gates (ADR 0032) | `config/base.yaml` `experiments` | fixed before results |
| Sigma-hat | interim EWMA, span 96 base bars | `fwd_returns.v1` | a VOL-006 selection on real data, approved by the owner and recorded in an ADR (C-18) |
| `ds_base.yaml` start | 2021-09-26 | `experiments/configs/ds_base.yaml` | broker history depth |
| Baseline board | fixed parameters (daily-bar rules, MA 20/50 and 50/200, 10 % vol target, 1,000 random-entry seeds); folds: expanding, ≥ 3 years training, 91-day tests, 1-day embargo | `experiments/configs/baselines/board.yaml`, ADR 0033, ADR 0034 | fixed before results; changes need an ADR |
| Random-walk forecast baseline | persistence of the latest completed bar return of the horizon's timeframe (`zero_return` covers the price random walk) | ADR 0033 | owner review of Sprint 4 |
| EDA parameters | bootstrap 1,000 resamples, block ≥ 5 trading days of bars and ≤ n/10; Hill tails 5 %; ≥ 20 lags (one trading day); Bonferroni family-wise 0.05 with cluster-robust Student-t intervals; LBMA windows −5/+30 min; VR q = 2, 4, 16, 92 on 15m; runs on 1h | `config/eda.yaml`, ADR 0038 | fixed before results; changes need an ADR |
| Statistical tests (STAT-001 … STAT-006) | level 0.05; ADF with AIC lags, KPSS level and trend, Zivot-Andrews 15 % trimming; Ljung-Box lags 1, 5, 10, 20 with Holm across lags; ARCH-LM lags 5, 10; variance ratios at 2 … 64 bars with Chow-Denning; ARMA models `ar1`, `arma11`, `ar_aic` (p ≤ 5 by AIC on training folds) against `zero_return` and `random_walk`, DM with Holm across horizons | `config/stats.yaml`, ADR 0043 | fixed before results; changes need an ADR |
| Volatility research (VOL-001 … VOL-006) | estimator window 20 bars, Wilder ATR 14; RV from 1m and 5m returns per hour and trading day; diurnal factor day-standardized, at least 20 training rows per bucket; benchmarks `rolling_22`, `ewma_0.94`, `ewma_0.97`, HAR (1, 5, 22 days; 1, 23, 115 hours), HAR floor 1 % of mean training RV; GARCH, GJR, EGARCH × normal, t, skewed t, zero mean, 1,000 EGARCH simulations; QLIKE primary, MCS 90 % (1,000 resamples, block 5), DM level 0.05 against HAR; selection default `ewma_0.94`, challengers' one-sided DM p-values Holm-adjusted before the 0.05 level (ADR 0046) | `config/volatility.yaml`, ADR 0044 | fixed before results; changes need an ADR |
| Horizon admission | cost-to-volatility bound 0.3 (plan default) on the overall mean ratio; horizons are TGT-002 labels 1m–1d measured from 1m bars on a 5-minute decision grid with `fwd_returns.v1`'s latency and fill delay; spread at the fills; slippage sigma-hat from the last 60 one-minute returns (causal fallback); financing at the mean of the long and short rates | `config/eda.yaml`, ADR 0037, ADR 0040 | owner review of the first real EDA; broker costs |

## Known issues and technical debt

- No real market data exists. Sprints 2–6 and 11 are tested only on synthetic data (Sprints 5 and
  6 also on simulated processes); nothing is validated on real data (C-8).
- The board runner does not yet implement the revised H-0001 (full-history rule evaluation with a
  fold-aligned view, 1d and 1h signal bars in one board, year and session slices): C-15.
- `config/horizons.yaml` does not exist, and nothing reads it yet: the target sets still emit all
  four default horizons until the admission list exists (ADR 0037).
- EDA speed on real minute data is unmeasured: the bootstrap draws 1,000 resamples of about 700k
  one-minute returns one at a time, and the Student-t fit uses the full series.
- The `SPREAD_OUTLIER` cleaning flag fires on rollover widening, and spread statistics use
  hourly buckets that blur short spikes; to be revisited in the DQ-008 review (ADR 0013).
- `base.v1` is an interim feature set (bar values, context bars, calendar columns) until FEAT-001
  (Sprint 7); sigma-hat is the interim EWMA of `fwd_returns.v1` until a VOL-006 selection on real
  data is approved and promoted (C-18). The Sprint 6 plan's "the selected `VolForecaster` serves
  sigma-hat to datasets" is therefore not done: `serve_sigma` exists, nothing is wired to datasets.
- Sprint 6 methods are validated on simulated processes only; none has run on real data, and
  there is no CLI for the statistical verdict report or the volatility board (C-18). Their speed on
  four years of real 1m bars is unmeasured (EGARCH multi-step forecasts are simulated, 1,000 paths
  per origin, in chunks).
- Realized measures leave out the return across the daily break and weekends (not intraday);
  only Yang-Zhang carries those gaps, so a sigma-hat from RV understates the risk of holding
  across a weekend. The diurnal factor buckets by time of day only, not by day of week.
- GARCH-family forecasts start their recursion from arch's backcast of the first 75 periods
  passed; they are causal from the 75th period on (every test period in walk-forward). The plain
  Ljung-Box on returns is reported next to the robust Q* but over-rejects under volatility
  clustering; only Q* may support a claim of return autocorrelation (ADR 0043).
- The trial counter does not compare recorded trials with a hypothesis's trial budget for any
  family (ADR 0042). A hypothesis can no longer be registered in a reserved model family
  (`linear_forecasts`, `volatility_models`; `xq.tracking.registry.RESERVED_FAMILIES`, ADR 0047),
  and the studies' own trials can never land in a strategy family (ADR 0046).
- Vault gate tokens can be verified but not issued until GATE-002 (Sprint 13);
  `xq validate --include-vault` uses an explicit confirmation flag until then.
- Parquet bytes depend on the pyarrow version, so a lockfile change can change dataset hashes
  without changing values (ADR 0017).
- Target computation reads a month of ticks at a time; memory and speed on four years of real
  ticks are unmeasured. The same holds for the baseline board (24 strategies and 24,000
  random-entry screens by default), although it reduces quotes to those the screener reads.
- PBO, SPA/Reality Check/Romano–Wolf, the multiple-testing corrections and five robustness
  measures (ROB-001, ROB-002, ROB-003, ROB-006, ROB-007) exist as library functions proven on
  simulated strategies. Nothing wires them into a report or a CLI yet (ROB-008,
  `xq validate-strategy`, GATE-001), and none has run on real data. Still not implemented: the R1
  gate's paired block-bootstrap test against the best baseline (the board stores each baseline's
  daily returns in `returns.parquet` for it), the Monte Carlo with risk rules (ROB-004) and noise
  injection (ROB-005).
- SPA and the Reality Check over-reject under strong serial dependence in short samples (AR(1)
  φ = 0.4 with 400 periods: 15 % and 20 % at a 10 % level); daily strategy returns are usually far
  less dependent (ADR 0054, C-24).
- BT-003's `drawdown_metrics` takes its first peak at the first day's equity, not the capital, so
  a drawdown that starts on day 1 is understated (equity 99, 98, 97 on 100 reports 2/99, not
  3/100). It feeds the R2 `oos_max_drawdown_max` gate; the robustness bootstrap (ROB-003) already
  starts from the capital. A fix is proposed as a separate task.
- Re-running the board with `xq baselines run` records its trials again (every evaluation counts),
  so the raw trial count grows with re-runs; the review flag fires when raw / effective exceeds
  10. `xq exp reproduce` does not: a reproduction's configurations are the original's trials.
- `xq exp reproduce` has a reproducer only for `baseline_board` runs; other kinds are refused by
  name.
- Undefined walk-forward metrics (NaN) are kept in results but not logged to the registry
  (`ef9bbd9`).
- Event-tier results are engineering tests on synthetic quotes with placeholder costs and a
  provisional risk profile; no forecasting model or regime model exists, so the signal engine has
  run only on a synthetic stub forecaster declared calibrated (SIGNAL-005).
- Risk halts stop new exposure, not the strategy's intents: while a halt holds, a strategy keeps
  sending intents that are rejected (all recorded). Consecutive losing round trips keep counting
  during a cooldown when risk-forced exits close at a loss, which extends the cooldown.
- The event tier replays every quote in pure Python: about 46,000 quotes a second (55,000
  synthetic quotes, 74 fills, placeholder costs, 1.2 s in this sandbox); its speed and memory on
  four years of real ticks are unmeasured. Each fill's slippage
  multiplier comes from a per-minute table built once per trading day (exact only because every
  configured boundary is on a whole minute; otherwise it falls back to the direct computation).
- Reconciliation and sizing: the event tier sizes through the risk engine (equity, stops,
  rounding down to the 0.01-lot step), the screener sizes fractional lots of capital at the fill's
  mid; the difference is reported as the sizing effect and the 5 % tolerance applies after it
  (ADR 0050). Tests of execution mechanics use a risk profile in which the requested exposure
  binds and no halt or breaker does; with the default profile the risk engine's sizes and
  refusals are explained differences.
- Exposure by session in the backtest report values positions at their latest fill's mid (the
  screener has no per-quote marks), a description rather than an attribution.
- Not started, deferred by plan: DQ-005 (feed consistency), DATA-011,
  DATA-012, BASE-004, EDA-007, STAT-004, STAT-005 (Sprint 8), STAT-007 (gated); SARIMA (only if
  EDA-004 finds a stable daily cycle) and FIGARCH (only if STAT-004 finds long memory) are not
  built; DATA-013 deferred by the owner (C-17).
- The raw-file permission test is skipped when the suite runs as root (it runs in CI, and in the
  Docker test stage, which runs as the non-root user).

## Status per phase

| Phase | Tasks done | Implemented | Tested (synthetic) | Validated on real data |
| --- | --- | --- | --- | --- |
| 0 Architecture | ARCH-001 … ARCH-008 | yes | yes (image verified in CI) | not applicable |
| 1 Market data | DATA-001 … DATA-010 | yes | yes | no |
| 2 Data quality | DQ-001 … DQ-004, DQ-006, DQ-007 (DQ-005, DQ-008 open) | yes | yes | no |
| 3 Datasets | DS-001 … DS-007 | yes | yes | no |
| 4 Exploratory research | EDA-001 … EDA-006 (EDA-007 in Sprint 8) | yes | yes (synthetic data, simulated processes) | no (no report on real data yet, C-16) |
| 5 Statistical time series | STAT-001, STAT-002, STAT-003, STAT-006, STAT-008 framework (STAT-004, STAT-005 in Sprint 8; STAT-007 gated) | yes | yes (simulated processes, recovery registry) | no (no verdict report on real data, C-18) |
| 6 Volatility research | VOL-001 … VOL-006 | yes | yes (simulated processes, recovery registry) | no (no volatility board on real data; nothing promoted, C-18) |
| 9 Targets | TGT-001, TGT-002 (TGT-003 … TGT-006 later) | yes | yes | no |
| 10 Baselines | BASE-001, BASE-002, BASE-003, BASE-005, BASE-006 (BASE-004 later) | yes | yes | no |
| 12 Walk-forward | WF-001, WF-002, WF-003, WF-006 (WF-004, WF-005 later) | yes | yes | no |
| 13 Backtesting | BT-001 … BT-010 | yes | yes (hand-computed trades, simulated quotes, reconciliation of the tiers) | no |
| 14 Risk engine | RISK-001 … RISK-006 | yes | yes (hand-computed cases, exact thresholds, property tests, architectural test, synthetic runs) | no |
| 15 Signal engine | SIGNAL-001 … SIGNAL-005 (the regime filter a pass-through until REG-007) | yes | yes (golden EV, filter cases, forecast-to-fill with a synthetic stub forecaster) | no |
| 16 Robustness research | ROB-001, ROB-002, ROB-003, ROB-006, ROB-007 (ROB-004, ROB-005, ROB-008 later) | yes | yes (simulated strategies with known truth: overfit vs genuine edges, planted edges, coverage; recovery registry) | no |
| 17 Statistical validation | VAL-001 … VAL-007 | yes | yes (published examples, independent implementations; VAL-003, VAL-004, VAL-006 on simulated noise, graded and overfit families) | no |
| 18 Experiment tracking | EXP-001 … EXP-006; reserved trial families (ADR 0047); `backtests` table (migration 0009) | yes | yes (a fixture board run reproduces) | not applicable until real research runs |
| All other phases | not started | no | no | no |
