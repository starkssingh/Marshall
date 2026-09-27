# Project status

Read this after `CLAUDE.md` at the start of every session. It is updated at the end of every
sprint and whenever a decision or carry-over item changes; anything decided in conversation is
recorded in an ADR and here in the same session. If a memory of an earlier conversation conflicts
with the repository, the repository wins.

- **Last updated:** 2026-09-27, after the owner's review of Sprint 5
- **Merged to `main`:** Sprints 1–5 with the revised H-0001 draft (PRs #2, #3, #6, #7, #8, #9).
  The fixes from the owner's review of PR #9 (EDA-006, ADR 0040, ADR 0041) are on branch
  `claude/wonderful-euler-v8orf5`, restarted from `main` because PR #9 was already merged, in a
  new pull request to `main`.

## Current sprint

- **Sprint:** 5 — exploratory research and horizon admission — **build-only, complete** (owner's
  instruction, ADR 0035): EDA-001, EDA-006, EDA-002, EDA-003, EDA-004, EDA-005 and EXP-005 are
  implemented and tested on synthetic data and on simulated processes with known properties
  (GARCH(1,1), AR(1), injected hour-of-week effects). DATA-013 is deferred until the owner decides
  whether a secondary feed is needed. The research half of the sprint — the EDA report on the
  discovery window of real data, the admission list in `config/horizons.yaml`, the hypotheses
  backlog and its pre-registrations — waits for real data (C-16). Nothing is validated on real data.
  After the owner's review, EDA-006 measures horizons with TGT-002's holding periods (1m bars,
  5-minute decision grid), prices the spread at the fills, reports medians beside the means, uses a
  causal sigma-hat fallback, keeps placeholder costs out of `config/horizons.yaml` without an
  explicit flag, and admits on the overall ratio only (ADR 0040, ADR 0041).
- **Working system:** `xq research eda --dataset <id> --hypothesis <H>` writes a deterministic EDA
  report (distributions, dependence, seasonality, trend and reversion, cost to volatility and the
  horizon admission list) on the discovery window only (ADR 0036–0038); `xq exp close` closes an
  experiment only with a written conclusion and appends it to `docs/research/log.md`, and
  `xq exp audit` lists experiments without one (ADR 0039). Demonstrated end to end on a synthetic
  three-week dataset only.
- **Next:** the owner reviews the pull request with the EDA-006 fixes. Claude implements the revised
  H-0001 in the board runner (C-15). With real data (C-8): the research half of Sprint 5 (C-16),
  including H-0000. Then Sprint 6 (STAT-001, STAT-002, STAT-003, STAT-006, STAT-008, VOL-001 …
  VOL-006, BASE-003), whose methods can likewise be built and simulation-tested first.
- **Not allowed yet:** running H-0001 (or any board) on real data before the owner has approved
  and registered it; generating an EDA report on real or pseudo-real data, or writing values to
  `config/horizons.yaml`, before the owner allows it (ADR 0035); pushing to `main`.

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
| C-16 | Research half of Sprint 5, after real data (C-8): fix `eda.discovery.end` from the data's depth; pre-register the standing descriptive hypothesis H-0000 (zero trial budget; the EXP-002 schema must first accept a zero budget) alongside H-0001, and run EDA under it (ADR 0041); run the EDA confirmatory; review it; write `config/horizons.yaml` with `xq research admit-horizons`; write `docs/research/hypotheses-backlog.md` and pre-register its top items | Sprint 5 (build-only) | Owner (data, window, approval), then Claude | open — blocked on C-8 and the owner's go-ahead |
| C-17 | DATA-013 secondary long-history adapter: build only if the owner decides a secondary feed is needed (depends on the broker's history depth) | Sprint 5 start | Owner (decision) | open |

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

Decided at the Sprint 4 hold point (ADR 0032): the evidence gates, the meaning of `1d`, decisions
taken while the market is closed, financing on both sides, and verifying the Docker image in CI.
Decided at the start of Sprint 5 (ADR 0035): the H-0001 revision (kept unregistered), a
build-only Sprint 5 with no EDA report on real or pseudo-real data and no `config/horizons.yaml`
values, and DATA-013 deferred. Decided at the Sprint 5 review (ADR 0040, ADR 0041): the six
EDA-006 changes; the H-0001 readings (lookbacks in signal-timeframe bars; the fold-aligned copy is
not a trial) and "EDA records no trials" approved; EDA runs belong to a standing descriptive
hypothesis H-0000 with a zero trial budget, registered alongside H-0001 once real data fixes the
windows; per-session admission is report-only, usable only through a pre-registered hypothesis.

## Provisional assumptions not yet confirmed

| Assumption | Value in use | Where | Confirmed by |
| --- | --- | --- | --- |
| Broker | unnamed | `config/base.yaml` `sources.mt5_primary` | owner naming the broker |
| Source clock | `NY+7` (UTC+2/+3, US DST dates) | ADR 0003, ADR 0004 | broker documentation, DQ-004 on real data |
| Contract terms | tick 0.01, 100 oz per lot, lot step 0.01, max 100 | `config/instruments/xauusd.yaml` | broker contract spec |
| Trading calendar | 18:00–17:00 New York, NYSE holidays, 13:30 early closes | ADR 0002, `config/sessions.yaml` | broker schedule |
| Costs (commission, slippage, financing) | placeholder model, PROVISIONAL: commission 3.5 USD/lot/side; slippage 0.5 bp + 0.1·σ̂₁ₘ, ×3 rollover window, ×2 US release; financing 6 %/yr long, 2 %/yr short (both a cost, required while provisional), act/360, triple Wednesday; spread fallback p90 | `config/costs/placeholder.yaml`, ADR 0029, ADR 0032 | broker terms, paper trading |
| Execution latency | 1 s (market time from ADR 0026) | `config/targets.yaml` `fwd_returns.v1`, cost model | BT-001, paper trading |
| Maximum fill delay | 300 s | `fwd_returns.v1`, cost model | ADR 0026: kept, provisional |
| Bar publication latency | 0 ms | `config/base.yaml` `bars` | live feed measurement |
| Quality thresholds | ratified provisional; one change allowed after DQ-008 | `config/quality.yaml`, ADR 0013 | DQ-008 review |
| Event windows | US release −5/+30 min; rollover 16:45–18:15 New York (ADR 0026) | `config/sessions.yaml` | EDA |
| Trial clustering | ρ 0.7, 60 common trading days; frozen with the gates (ADR 0032) | `config/base.yaml` `experiments` | fixed before results |
| Sigma-hat | interim EWMA, span 96 base bars | `fwd_returns.v1` | VOL-006 (Sprint 6) |
| `ds_base.yaml` start | 2021-09-26 | `experiments/configs/ds_base.yaml` | broker history depth |
| Baseline board | fixed parameters (daily-bar rules, MA 20/50 and 50/200, 10 % vol target, 1,000 random-entry seeds); folds: expanding, ≥ 3 years training, 91-day tests, 1-day embargo | `experiments/configs/baselines/board.yaml`, ADR 0033, ADR 0034 | fixed before results; changes need an ADR |
| Random-walk forecast baseline | persistence of the latest completed bar return of the horizon's timeframe (`zero_return` covers the price random walk) | ADR 0033 | owner review of Sprint 4 |
| EDA parameters | bootstrap 1,000 resamples, block ≥ 5 trading days of bars and ≤ n/10; Hill tails 5 %; ≥ 20 lags (one trading day); Bonferroni family-wise 0.05 with cluster-robust Student-t intervals; LBMA windows −5/+30 min; VR q = 2, 4, 16, 92 on 15m; runs on 1h | `config/eda.yaml`, ADR 0038 | fixed before results; changes need an ADR |
| Horizon admission | cost-to-volatility bound 0.3 (plan default) on the overall mean ratio; horizons are TGT-002 labels 1m–1d measured from 1m bars on a 5-minute decision grid with `fwd_returns.v1`'s latency and fill delay; spread at the fills; slippage sigma-hat from the last 60 one-minute returns (causal fallback); financing at the mean of the long and short rates | `config/eda.yaml`, ADR 0037, ADR 0040 | owner review of the first real EDA; broker costs |

## Known issues and technical debt

- No real market data exists. Sprints 2–5 are tested only on synthetic data (Sprint 5 also on
  simulated processes); nothing is validated on real data (C-8).
- The board runner does not yet implement the revised H-0001 (full-history rule evaluation with a
  fold-aligned view, 1d and 1h signal bars in one board, year and session slices): C-15.
- `config/horizons.yaml` does not exist, and nothing reads it yet: the target sets still emit all
  four default horizons until the admission list exists (ADR 0037).
- EDA speed on real minute data is unmeasured: the bootstrap draws 1,000 resamples of about 700k
  one-minute returns one at a time, and the Student-t fit uses the full series.
- The `SPREAD_OUTLIER` cleaning flag fires on rollover widening, and spread statistics use
  hourly buckets that blur short spikes; to be revisited in the DQ-008 review (ADR 0013).
- `base.v1` is an interim feature set (bar values, context bars, calendar columns) until FEAT-001
  (Sprint 7); sigma-hat is an interim EWMA until VOL-006 (Sprint 6).
- Vault gate tokens can be verified but not issued until GATE-002 (Sprint 13);
  `xq validate --include-vault` uses an explicit confirmation flag until then.
- Parquet bytes depend on the pyarrow version, so a lockfile change can change dataset hashes
  without changing values (ADR 0017).
- Target computation reads a month of ticks at a time; memory and speed on four years of real
  ticks are unmeasured. The same holds for the baseline board (24 strategies and 24,000
  random-entry screens by default), although it reduces quotes to those the screener reads.
- The R1 gate's paired block-bootstrap test against the best baseline, PBO, SPA, the Monte Carlo
  and robustness measures are not implemented yet; the board stores each baseline's daily returns
  (`returns.parquet`) so the paired test can use them (GATE-001 and the research sprints).
- Re-running the board records its trials again (every evaluation counts), so the raw trial count
  grows with re-runs; the review flag fires when raw / effective exceeds 10.
- Undefined walk-forward metrics (NaN) are kept in results but not logged to the registry
  (`ef9bbd9`).
- Not started, deferred by plan: EXP-006 (reproduce), DQ-005 (feed consistency), DATA-011,
  DATA-012, BASE-003, BASE-004, EDA-007 (Sprint 8); DATA-013 deferred by the owner (C-17).
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
| 9 Targets | TGT-001, TGT-002 (TGT-003 … TGT-006 later) | yes | yes | no |
| 10 Baselines | BASE-001, BASE-002, BASE-005, BASE-006 (BASE-003, BASE-004 later) | yes | yes | no |
| 12 Walk-forward | WF-001, WF-002, WF-003, WF-006 (WF-004, WF-005 later) | yes | yes | no |
| 13 Backtesting | BT-001, BT-002, BT-003 (BT-004 … BT-010 later) | yes | yes | no |
| 17 Statistical validation | VAL-001, VAL-002, VAL-005, VAL-007 (VAL-003, VAL-004, VAL-006 later) | yes | yes | no |
| 18 Experiment tracking | EXP-001 … EXP-005 (EXP-006 later) | yes | yes | not applicable until real research runs |
| All other phases | not started | no | no | no |
