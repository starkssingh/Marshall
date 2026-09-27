# Project status

Read this after `CLAUDE.md` at the start of every session. It is updated at the end of every
sprint and whenever a decision or carry-over item changes; anything decided in conversation is
recorded in an ADR and here in the same session. If a memory of an earlier conversation conflicts
with the repository, the repository wins.

- **Last updated:** 2026-09-27, end of Sprint 4
- **Merged to `main`:** Sprints 1–3 (PRs #2, #3, #6) and the first part of Sprint 4, WF-001 to
  VAL-005 (PR #7). The rest of Sprint 4 (VAL-007, the owner's hold-point answers, BASE-001,
  BASE-002, BASE-005) is on branch `claude/blissful-cori-1wvkn4`, in a pull request to `main`
  awaiting the owner's review.

## Current sprint

- **Sprint:** 4 — evaluation spine — **complete**: all 15 tasks implemented and tested on
  synthetic data (WF-001, WF-006, WF-002, WF-003, BT-001, BT-002, BT-003, BASE-006, VAL-001,
  VAL-002, VAL-005, VAL-007, BASE-001, BASE-002, BASE-005). Nothing is validated on real data.
- **Working system:** `xq baselines run --dataset <id>` produces a walk-forward, net-of-cost
  baseline board with Sharpe intervals and DSR using the registered trial count (ADR 0034),
  demonstrated end to end on a synthetic three-week dataset only.
- **Next:** the owner reviews the Sprint 4 pull request and the H-0001 draft (C-14). Then
  Sprint 5 (EDA-001, EDA-006, EDA-002 … EDA-005, EXP-005, DATA-013). Its research tasks run on the
  discovery window of real data, so they are blocked on C-8; its engineering parts can be built
  and tested on synthetic data.
- **Not allowed yet:** running H-0001 (or any board) on real data before the owner has reviewed
  and registered H-0001; pushing to `main`.

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
| C-14 | Review the draft `experiments/hypotheses/H-0001.yaml` (the baseline board), then register it before any real-data run | Sprint 4 | Owner | open |

## Open owner decisions

| Question | Options | Default in use |
| --- | --- | --- |
| Execution broker, venue and primary feed | MT5 broker (to be named), OANDA, cTrader | `mt5_primary` placeholder (ADR 0004): MT5 tick export, server clock `NY+7` |
| Secondary long-history feed if broker history is short (DATA-013) | Dukascopy, none | none |
| Broker cost terms (commission, financing rates, triple day, holiday financing) | broker's published terms | placeholder cost model (ADR 0029), financing a cost on both sides (ADR 0032) |
| Annualization of daily statistics (gates: "252, provisional") | 252; the calendar's open trading days (257–259 a year in 2022–2025) | 252 (`backtest.periods_per_year`) |
| Boundary rule of each gate threshold (`>` vs `>=`) | as tabled in ADR 0032 (plan wording where it states one; otherwise `_min` at least, `_max` at most, Sharpe floors strict) | ADR 0032 table |
| Account currency | USD, other | USD (plan default) |
| Research horizon focus | 15m–1d, other | 15m–1d; four horizons kept until EDA-006 (ADR 0026) |
| Risk budget | per-trade risk, drawdown halt | 0.5 % per trade, halt at 15 % drawdown (plan default; the gates' 0.15 drawdown limits match it) |
| Vault | holdout start | `2025-09-25T21:00:00Z`, the last 12 months at project start (fixed) |

Decided at the Sprint 4 hold point (ADR 0032): the evidence gates, the meaning of `1d`, decisions
taken while the market is closed, financing on both sides, and verifying the Docker image in CI.

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

## Known issues and technical debt

- No real market data exists. Sprints 2–4 are tested only on synthetic data; nothing is
  validated on real data (C-8).
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
- Not started, deferred by plan: EXP-005 (conclusions table and research log), EXP-006
  (reproduce), DQ-005 (feed consistency), DATA-011 to DATA-013, BASE-003, BASE-004.
- The raw-file permission test is skipped when the suite runs as root (it runs in CI, and in the
  Docker test stage, which runs as the non-root user).

## Status per phase

| Phase | Tasks done | Implemented | Tested (synthetic) | Validated on real data |
| --- | --- | --- | --- | --- |
| 0 Architecture | ARCH-001 … ARCH-008 | yes | yes (image verified in CI) | not applicable |
| 1 Market data | DATA-001 … DATA-010 | yes | yes | no |
| 2 Data quality | DQ-001 … DQ-004, DQ-006, DQ-007 (DQ-005, DQ-008 open) | yes | yes | no |
| 3 Datasets | DS-001 … DS-007 | yes | yes | no |
| 9 Targets | TGT-001, TGT-002 (TGT-003 … TGT-006 later) | yes | yes | no |
| 10 Baselines | BASE-001, BASE-002, BASE-005, BASE-006 (BASE-003, BASE-004 later) | yes | yes | no |
| 12 Walk-forward | WF-001, WF-002, WF-003, WF-006 (WF-004, WF-005 later) | yes | yes | no |
| 13 Backtesting | BT-001, BT-002, BT-003 (BT-004 … BT-010 later) | yes | yes | no |
| 17 Statistical validation | VAL-001, VAL-002, VAL-005, VAL-007 (VAL-003, VAL-004, VAL-006 later) | yes | yes | no |
| 18 Experiment tracking | EXP-001 … EXP-004 (EXP-005, EXP-006 later) | yes | yes | not applicable until real research runs |
| All other phases | not started | no | no | no |
