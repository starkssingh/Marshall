# Project status

Read this after `CLAUDE.md` at the start of every session. It is updated at the end of every
sprint and whenever a decision or carry-over item changes. Anything decided in conversation is
recorded in an ADR and here in the same session. If a memory of an earlier conversation conflicts
with the repository, the repository wins.

- **Last updated:** 2026-09-27, Sprint 4 at the VAL-007 hold point.
- **Merged to `main`:** Sprints 1–3 (PRs #2, #3, #6).
- **Branch in progress:** `sprint-4` (from `main` at `b64f059`), not yet proposed for merge.

## Current sprint

- **Sprint:** 4, the evaluation spine. Synthetic data only.
- **Next task:** VAL-007. It is on hold until the owner approves the proposed `config/gates.yaml`.
- **Sprint 4 order:** WF-001, WF-006, WF-002, WF-003, BT-001, BT-002, BT-003, BASE-006, VAL-001,
  VAL-002, VAL-005, VAL-007, BASE-001, BASE-002, BASE-005, then a PR to `main`.
- **Done so far, with commits:**
  - WF-001 `1f652a9`, WF-006 `86b48b1`, WF-002 `a63a305`, WF-003 `dd29a07`;
  - BT-001 `4838820`, BT-002 `4fdb503`, BT-003 `f565cdc`;
  - BASE-006 `2eb2ac6`;
  - VAL-001 `0f16b1f`, VAL-002 `6b884db`, VAL-005 `7b46fe9`.
- **Remaining after approval:** VAL-007, BASE-001, BASE-002, BASE-005, the end-of-sprint STATUS
  update, and the PR.
- **Not allowed this sprint:** running H-0001 on real data; pushing to `main`.

## Carry-over items from reviews

| ID | Item | From | Owner | Closed by |
| --- | --- | --- | --- | --- |
| C-1 | Record the Sprint 3 review decisions in one ADR | Sprint 3 review | Claude | `4c67cfe` (ADR 0026) |
| C-2 | Fill-delay diagnostic: count fills delayed > 5 s in target-build output | Sprint 3 review | Claude | `5ebfa87` |
| C-3 | Rollover window 16:45–18:15 America/New_York (US release window unchanged) | Sprint 3 review | Claude | `3d141a1` |
| C-4 | Trial clustering: keep ρ 0.7, require 60 common daily points | Sprint 3 review | Claude | `f15ef30` |
| C-5 | Trading-time horizons (market-open minutes only) and a `crosses_close` target column; leakage tests and `label_end` checks updated | Sprint 3 review | Claude | `523821d` |
| C-6 | Rebuild the Docker image and run the suite inside it (`scipy` was added without re-checking) | Sprint 3 review | Claude, then owner | open, blocked: building the image in the Claude sandbox needs the session proxy's CA inside the build, which is not permitted. The owner chooses a route (see open decisions) |
| C-7 | `resample_causal` must respect availability (latency argument, test with latency > 0) | Sprint 3 review | Claude | `157a78c` |
| C-8 | Run `xq validate` on ≥ 1 year of real broker ticks, then the DQ-008 human review | Sprint 2 | Owner (data), then Claude | open, blocked on real data |

## Open owner decisions

| Question | Options | Default in use |
| --- | --- | --- |
| Evidence gate thresholds (`config/gates.yaml`, VAL-007) | approve the proposal, or change it | none; the proposal (plan R1–R4 defaults plus four additions) awaits approval |
| … trial count used by DSR and SPA | raw registered count, effective (clustered) count | proposed: raw, the stricter bar; the effective count is reported too |
| … margin over the best baseline (R1) | 0 Sharpe (the paired bootstrap p < 0.10 is the whole bar), an extra Sharpe margin | proposed: 0 |
| … test sidedness and bootstrap | one-sided "better than" tests; 10,000 stationary-bootstrap draws with 5-day mean blocks | proposed as stated |
| Meaning of the `1d` horizon in trading time | 24 market hours (one trading day + 1 h), 23 market hours (one trading day) | 24 market hours, the literal reading of ADR 0026 |
| Decisions taken while the market is closed | enter at the reopen, no trade | enter at the reopen plus latency (ADR 0026) |
| How to verify the Docker image (C-6) | a CI job that builds a `test` target and runs the suite on GitHub Actions; the owner builds and runs it locally; allow sandbox builds with the proxy CA | none yet; the image was last built in Sprint 1 |
| Execution broker, venue and primary feed | MT5 broker (to be named), OANDA, cTrader | `mt5_primary` placeholder (ADR 0004): MT5 tick export, server clock `NY+7` |
| Broker cost terms (commission, financing rates, triple day, holiday financing) | the broker's published terms | placeholder cost model (ADR 0029) |
| Secondary long-history feed if broker history is short (DATA-013) | Dukascopy, none | none |
| Account currency | USD, other | USD (plan default) |
| Research horizon focus | 15m–1d, other | 15m–1d; the four horizons are kept until EDA-006 (ADR 0026) |
| Risk budget | per-trade risk, drawdown halt | 0.5 % per trade, halt at 15 % drawdown (plan default) |
| Vault | holdout start | `2025-09-25T21:00:00Z`, the last 12 months at project start (fixed) |

## Provisional assumptions not yet confirmed

| Assumption | Value in use | Where | Confirmed by |
| --- | --- | --- | --- |
| Broker | unnamed | `config/base.yaml` `sources.mt5_primary` | the owner naming the broker |
| Source clock | `NY+7` (UTC+2/+3, US DST dates) | ADR 0003, ADR 0004 | broker documentation, DQ-004 on real data |
| Contract terms | tick 0.01, 100 oz per lot, lot step 0.01, max 100 | `config/instruments/xauusd.yaml` | broker contract spec |
| Trading calendar | 18:00–17:00 New York, NYSE holidays, 13:30 early closes | ADR 0002, `config/sessions.yaml` | broker schedule |
| Costs (commission, slippage, financing) | placeholder model, PROVISIONAL (see below) | `config/costs/placeholder.yaml`, ADR 0029 | broker terms, paper trading |
| Execution latency | 1 s of market time | `fwd_returns.v1` in `config/targets.yaml`; `latency_ms` in the cost model | paper trading |
| Maximum fill delay | 300 s (kept, provisional, ADR 0026) | `fwd_returns.v1`; the cost model | paper trading |
| Bar publication latency | 0 ms | `config/base.yaml` `bars` | live feed measurement |
| Backtest capital and annualization | 100,000 USD; 252 periods per year | `config/base.yaml` `backtest` | owner review |
| Quality thresholds | ratified provisional; one change allowed after DQ-008 | `config/quality.yaml`, ADR 0013 | DQ-008 review |
| Event windows | US release −5/+30 min; rollover 16:45–18:15 New York (ADR 0026) | `config/sessions.yaml` | EDA |
| Trial clustering | ρ 0.7, 60 common trading days (ADR 0026) | `config/base.yaml` `experiments` | fixed before results |
| Sigma-hat | interim EWMA, span 96 base bars | `fwd_returns.v1` | VOL-006 (Sprint 6) |
| `ds_base.yaml` start | 2021-09-26 | `experiments/configs/ds_base.yaml` | broker history depth |

The placeholder cost values are:
- commission 3.5 USD per lot per side;
- slippage 0.5 bp + 0.1 × σ̂ of 1-minute returns, ×3 in the rollover window and ×2 in the US
  data release window;
- financing 6 %/yr long and 2 %/yr short, act/360, triple on Wednesday;
- spread fallback at the hour-of-week p90.

## Known issues and technical debt

- **No real market data.** Sprints 2–4 are tested only on synthetic data, and nothing is
  validated on real data (C-8).
- **Every backtest result is screening only.** The cost model is a placeholder (ADR 0029) until
  the broker's terms replace it.
- **VAL-001 has no published reference.** Its standard errors are checked by closed forms and
  Monte Carlo; no published table was reproduced. VAL-002 does reproduce the published DSR
  example.
- **The screener is simplified.** It uses exposures relative to capital, with no lot rounding,
  margin or risk engine (ADR 0030). The event-driven tier (BT-004 onward) adds them, and BT-009
  reconciles the two tiers.
- **Walk-forward caveats (ADR 0028):**
  - A cached fold is keyed on the model's `code_version`. A behaviour change without a version
    bump would serve stale folds, so code review must check it.
  - A selected grid candidate is not refitted on training plus validation data; WF-004 may
    revisit this.
- **Diebold–Mariano needs the horizon.** A horizon-1 test over-rejects when loss differences are
  autocorrelated, so callers must pass the forecast horizon (ADR 0031).
- **Cleaning and spread statistics.** The `SPREAD_OUTLIER` flag fires on rollover widening, and
  hourly spread buckets blur short spikes. Both are revisited in the DQ-008 review (ADR 0013).
- **Interim estimates.** `base.v1` is an interim feature set until FEAT-001 (Sprint 7), and σ̂ is
  an interim EWMA until VOL-006 (Sprint 6).
- **Vault tokens.** They can be verified but not issued until GATE-002 (Sprint 13).
  `xq validate --include-vault` needs an explicit confirmation flag until then.
- **Dataset hashes and pyarrow.** Parquet bytes depend on the pyarrow version, so a lockfile
  change can change dataset hashes without changing values (ADR 0017).
- **Unmeasured at scale.** Target computation reads a month of ticks at a time; its memory use
  and speed on four years of real ticks are unmeasured.
- **Deferred by plan, not started:** EXP-005 (conclusions table and research log), EXP-006
  (reproduce), DQ-005 (feed consistency), DATA-011 to DATA-013.
- **Skipped as root.** The raw-file permission test is skipped when the suite runs as root. It
  runs in CI.
- **Docker image is stale.** It has not been rebuilt since `scipy` was added (C-6), and cannot be
  built inside the Claude sandbox.

## Status per phase

| Phase | Tasks done | Implemented | Tested (synthetic) | Validated on real data |
| --- | --- | --- | --- | --- |
| 0 Architecture | ARCH-001 … ARCH-008 | yes | yes | not applicable |
| 1 Market data | DATA-001 … DATA-010 | yes | yes | no |
| 2 Data quality | DQ-001 … DQ-004, DQ-006, DQ-007 (DQ-005, DQ-008 open) | yes | yes | no |
| 3 Datasets | DS-001 … DS-007 | yes | yes | no |
| 9 Targets | TGT-001, TGT-002 (TGT-003 … TGT-006 later) | yes | yes | no |
| 10 Baselines | BASE-006 (BASE-001, BASE-002, BASE-005 after the gate approval; BASE-003, BASE-004 later) | partly | partly | no |
| 12 Walk-forward | WF-001, WF-002, WF-003, WF-006 (WF-004, WF-005 later) | yes | yes | no |
| 13 Backtesting | BT-001, BT-002, BT-003 (BT-004 … BT-010 later) | yes | yes, with placeholder costs | no |
| 17 Validation | VAL-001, VAL-002, VAL-005 (VAL-007 on hold; VAL-003, VAL-004, VAL-006 later) | yes | yes | not applicable until real research runs |
| 18 Experiment tracking | EXP-001 … EXP-004 (EXP-005, EXP-006 later) | yes | yes | not applicable until real research runs |
| All other phases | not started | no | no | no |
