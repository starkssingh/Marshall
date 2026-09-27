# Project status

Read this after `CLAUDE.md` at the start of every session. It is updated at the end of every
sprint and whenever a decision or carry-over item changes; anything decided in conversation is
recorded in an ADR and here in the same session. If a memory of an earlier conversation conflicts
with the repository, the repository wins.

- **Last updated:** 2026-09-26, start of Sprint 4 (before any Sprint 4 task)
- **Merged to `main`:** Sprints 1–3 (PRs #2, #3, #6)

## Current sprint

- **Sprint:** 4 — evaluation spine (branch `sprint-4`, from `main` at `b64f059`).
- **Next task:** the Sprint 3 review carry-overs below C-7, then WF-001 (C-6 blocked, see below).
- **Sprint 4 order:** WF-001, WF-006, WF-002, WF-003, BT-001, BT-002, BT-003, BASE-006, VAL-001,
  VAL-002, VAL-005, VAL-007, BASE-001, BASE-002, BASE-005 — synthetic data only.
- **Hold point:** before VAL-007, the proposed `config/gates.yaml` goes to the owner, and work waits
  for approval.
- **Not allowed this sprint:** running H-0001 on real data; pushing to `main`.

## Carry-over items from reviews

| ID | Item | From | Owner | Closed by |
| --- | --- | --- | --- | --- |
| C-1 | Record the Sprint 3 review decisions in one ADR | Sprint 3 review | Claude | `4c67cfe` (ADR 0026) |
| C-2 | Fill-delay diagnostic: count fills delayed > 5 s in target-build output | Sprint 3 review | Claude | `5ebfa87` |
| C-3 | Rollover window 16:45–18:15 America/New_York (US release window unchanged) | Sprint 3 review | Claude | `3d141a1` |
| C-4 | Trial clustering: keep ρ 0.7, require 60 common daily points | Sprint 3 review | Claude | `f15ef30` |
| C-5 | Trading-time horizons (market-open minutes only) and a `crosses_close` target column; leakage tests and `label_end` checks updated | Sprint 3 review | Claude | `523821d` |
| C-6 | Rebuild the Docker image and run the suite inside it (`scipy` added unchecked) | Sprint 3 review | Claude, then owner | open — blocked: image builds in the Claude sandbox need the session proxy's CA inside the build, which is not permitted; owner to choose a route (see open decisions) |
| C-7 | `resample_causal` must respect availability (latency argument, test with latency > 0) | Sprint 3 review | Claude | open |
| C-8 | Run `xq validate` on ≥ 1 year of real broker ticks, then the DQ-008 human review | Sprint 2 | Owner (data), then Claude | open — blocked on real data |

## Open owner decisions

| Question | Options | Default in use |
| --- | --- | --- |
| Execution broker, venue and primary feed | MT5 broker (to be named), OANDA, cTrader | `mt5_primary` placeholder (ADR 0004): MT5 tick export, server clock `NY+7` |
| Secondary long-history feed if broker history is short (DATA-013) | Dukascopy, none | none |
| Evidence gate thresholds (`config/gates.yaml`) | to be proposed before VAL-007 | none yet |
| Account currency | USD, other | USD (plan default) |
| Research horizon focus | 15m–1d, other | 15m–1d; four horizons kept until EDA-006 (ADR 0026) |
| Meaning of the `1d` horizon in trading time | 24 market hours (one trading day + 1 h), 23 market hours (one trading day) | 24 market hours, the literal reading of ADR 0026 |
| Risk budget | per-trade risk, drawdown halt | 0.5 % per trade, halt at 15 % drawdown (plan default) |
| How to verify the Docker image (C-6) | a CI job that builds a `test` target and runs the suite on GitHub Actions; the owner builds and runs it locally; allow sandbox builds with the proxy CA | none yet; the image was last built in Sprint 1 |
| Vault | holdout start | `2025-09-25T21:00:00Z`, the last 12 months at project start (fixed) |

## Provisional assumptions not yet confirmed

| Assumption | Value in use | Where | Confirmed by |
| --- | --- | --- | --- |
| Broker | unnamed | `config/base.yaml` `sources.mt5_primary` | owner naming the broker |
| Source clock | `NY+7` (UTC+2/+3, US DST dates) | ADR 0003, ADR 0004 | broker documentation, DQ-004 on real data |
| Contract terms | tick 0.01, 100 oz per lot, lot step 0.01, max 100 | `config/instruments/xauusd.yaml` | broker contract spec |
| Trading calendar | 18:00–17:00 New York, NYSE holidays, 13:30 early closes | ADR 0002, `config/sessions.yaml` | broker schedule |
| Costs (commission, slippage, financing) | not yet modelled | BT-001 (Sprint 4) | broker terms, paper trading |
| Execution latency | 1 s (market time from ADR 0026) | `config/targets.yaml` `fwd_returns.v1` | BT-001, paper trading |
| Decisions taken while the market is closed | entered at the reopen (plus latency) | `xq.targets.returns`, ADR 0026 | owner review of Sprint 4 |
| Maximum fill delay | 300 s | `fwd_returns.v1` | ADR 0026: kept, provisional |
| Bar publication latency | 0 ms | `config/base.yaml` `bars` | live feed measurement |
| Quality thresholds | ratified provisional; one change allowed after DQ-008 | `config/quality.yaml`, ADR 0013 | DQ-008 review |
| Event windows | US release −5/+30 min; rollover 16:45–18:15 New York (ADR 0026) | `config/sessions.yaml` | EDA |
| Trial clustering | ρ 0.7, 60 common trading days (ADR 0026) | `config/base.yaml` `experiments` | fixed before results |
| Sigma-hat | interim EWMA, span 96 base bars | `fwd_returns.v1` | VOL-006 (Sprint 6) |
| `ds_base.yaml` start | 2021-09-26 | `experiments/configs/ds_base.yaml` | broker history depth |

## Known issues and technical debt

- No real market data exists. Sprints 2 and 3 are tested only on synthetic data; nothing is
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
  ticks are unmeasured.
- Not started, deferred by plan: EXP-005 (conclusions table and research log), EXP-006
  (reproduce), DQ-005 (feed consistency), DATA-011 to DATA-013.
- The raw-file permission test is skipped when the suite runs as root (it runs in CI).
- The Docker image has not been rebuilt since `scipy` was added (C-6); it cannot be built inside the
  Claude sandbox without trusting the session proxy's CA in the build, which is not permitted.

## Status per phase

| Phase | Tasks done | Implemented | Tested (synthetic) | Validated on real data |
| --- | --- | --- | --- | --- |
| 0 Architecture | ARCH-001 … ARCH-008 | yes | yes | not applicable |
| 1 Market data | DATA-001 … DATA-010 | yes | yes | no |
| 2 Data quality | DQ-001 … DQ-004, DQ-006, DQ-007 (DQ-005, DQ-008 open) | yes | yes | no |
| 3 Datasets | DS-001 … DS-007 | yes | yes | no |
| 9 Targets | TGT-001, TGT-002 (TGT-003 … TGT-006 later) | yes | yes | no |
| 18 Experiment tracking | EXP-001 … EXP-004 (EXP-005, EXP-006 later) | yes | yes | not applicable until real research runs |
| 12 Walk-forward, 13 Backtesting, 10 Baselines, 17 Validation | Sprint 4 in progress | no | no | no |
| All other phases | not started | no | no | no |
