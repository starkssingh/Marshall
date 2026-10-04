# Project status

Read this after `CLAUDE.md` at the start of every session. It is updated at the end of every
sprint and whenever a decision or carry-over item changes; anything decided in conversation is
recorded in an ADR and here in the same session. If a memory of an earlier conversation conflicts
with the repository, the repository wins.

- **Last updated:** 2026-10-04, at the end of the session on branch
  `claude/bold-heisenberg-c0p3bj` (branched from `main` after the C-15 session was merged in
  PR #18): the owner's decisions on C-26, C-27 and C-28 (ADR 0062), then Sprint 7, build-only,
  part 1 (ADR 0063), in review.
- **Merged to `main`:** Sprints 1–6, 9, 11, 12 A, 12 B and 13, the Dukascopy data session and the
  C-15 session, with the revised H-0001 draft and the Sprint 5 and Sprint 6 review fixes (PRs #2,
  #3, #6, #7, #8, #9, #10, #11, #12, #13, #14, #15, #16, #17, #18).
- **Where sessions run:** real-data sessions run in Claude Code on the owner's Mac, where the data
  is (`data/` is git-ignored and never leaves that machine). Cloud sessions work on synthetic data
  only (ADR 0062).

## Current sprint

- **This session:** the owner's decisions on C-26, C-27 and C-28 (ADR 0062), then Sprint 7,
  build-only, part 1 (ADR 0063) — **complete, in review on branch
  `claude/bold-heisenberg-c0p3bj`, synthetic data only**. One commit each:
  - ADR 0062 and this STATUS (`144168c`): C-28 confirmed as built; C-26 and C-27 decided;
  - C-26: the dukascopy-node CSV route with the corrected flags `-r 3 -re -fr` and the owner's
    monthly loop in [`docs/runbooks/real-data.md`](runbooks/real-data.md) (`fcfa9e1`, the loop
    checked against a stub `npx` only); `ds_base` from 2015-01-01 (`d8da183`);
  - C-27 (3): promotion to `paper` needs R3 recomputed on the event tier, in `promote` and in the
    database (migration 0016, `0704849`);
  - ARCH-004: matplotlib, PIL and fontTools held at WARNING (`55bf8d1`);
  - TGT-003 future realized volatility (`b39a174`), TGT-004 MFE and MAE in sigma units
    (`d2fefbf`), TGT-005 triple-barrier labels (`fb322f4`), TGT-006 derived labels and uniqueness
    weights (`8ebccac`), a dataset built with each new target set (`0e477d6`);
  - WF-004 the monthly retraining schedule and stitching (`76b52a0`), WF-005 the walk-forward
    report with its decay regression and `xq exp wf-report` (`6528380`);
  - this STATUS, ADR 0063 and the README.
  - Proven on synthetic data: every new target passes the leakage harness (the bar-resolved
    barrier and the uniqueness weights too; the harness caught a first barrier version whose
    early labels needed the later exit fill); barrier labels recover known hit times, also
    against a brute-force search on random paths; a bar touching both barriers is a flagged stop
    while ticks resolve it exactly; the uniqueness weights match the hand example
    (0.75, 0.625, 0.75); the stitched out-of-sample series has no overlaps or gaps; the
    walk-forward report finds a planted decay and runs end to end on a synthetic board run.
    **Nothing has run on real data**, and no target set but `fwd_returns.v1` is named by a
    dataset spec.
  - Sprint 7 part 2 (FEAT-001 … FEAT-006, FEAT-008) is next. The readings for review are C-30.
- **Previous session:** C-15, the revised H-0001 in the board runner (ADR 0061) — **complete,
  merged in PR #18, synthetic data only**. Rules on `signal_timeframes: [1d, 1h]`, 36 strategies
  (H-0001's budget); each rule evaluated over its full history after its warm-up, the
  fold-aligned view kept; descriptive year and session slices. Its readings were confirmed in
  C-28. H-0001 is **not registered**.
- **Earlier:** Sprint 13 (the registry and the release gates, ADR 0060, PR #17) and the Dukascopy
  primary feed (ADR 0057, PR #16), both synthetic only; their open points were decided in C-27
  and C-26.
- **Owner's next step (C-8):** download and ingest on your Mac, from the repository root, with
  the dukascopy-node CSV route (ADR 0062): the monthly loop from 2014-01 in
  [`docs/runbooks/real-data.md`](runbooks/real-data.md) (`-r 3 -re -fr`, one CSV per month,
  partial files deleted on failure, `caffeinate`, a log). Market data stays under `data/`, which
  is git-ignored. March 2024 has already run end to end (328 pass / 25 warn / 3 fail, reported by
  the owner).

  ```bash
  uv sync
  uv run xq ingest --source dukascopy --path data/downloads/csv
  ```

  - Then the C-8 session runs in Claude Code on the owner's Mac, where `data/` is:

    ```bash
    uv run xq clean --source dukascopy
    uv run xq build-bars --source dukascopy
    uv run xq spread-stats --source dukascopy
    uv run xq validate --source dukascopy
    ```

    `xq validate` grades every ingested trading day before the vault (`--start`/`--end` narrow
    it); the report is written to `reports/quality/<run id>/report.md`. That session reads the
    report per check and per year, compares the calendar with Dukascopy's real hours (ADR 0057,
    decision 4; the March 2024 observation below), then prepares the DQ-008 human review. It
    runs nothing else on the data.
  - **Observation for DQ-004 / DQ-008** (the calendar is not changed yet, ADR 0062): Dukascopy
    XAUUSD's last Friday tick on 2024-03-01 was at 20:59:59 UTC, an hour before the calendar's
    22:00 UTC weekly close (17:00 New York, EST).
- **Working system:** library code and CLI, exercised by the tests.
  - **Event backtester** (`run_event_backtest`, tick or bar mode). It runs the whole chain the
    plan prescribes:
    - forecasts go to the `SignalEngine`: a strategy defined entirely by YAML, uncalibrated,
      stale and filtered forecasts refused, EV in sigma units with a conservative variant, and a
      `SignalRecord` for every candidate;
    - a `TradeIntent` then passes session constraints and `RiskEngine.evaluate`: pure, risk state
      rebuilt from the ledger, sizing on the edge per unit of risk with drawdown scaling, the
      stop policy, halts at their exact thresholds, caps, the kill switch and data-health
      breakers, exits always approved;
    - an approved decision becomes an `OrderIntent`, only from a decision the risk engine issued
      (enforced at runtime and by an AST architectural test), and goes through the
      `SimulatedBroker`, the `Portfolio` and the `Ledger`.

    There is no CLI for event backtests yet: they wait for real data and a candidate.
  - **Validation and robustness library** (Sprint 9): PBO, SPA, Reality Check and Romano–Wolf,
    Holm and Benjamini–Hochberg, parameter perturbation, cost stress, block bootstrap, slicing and
    execution delay.
  - **Added in Sprint 12 B:**
    - ROB-004, Monte Carlo equity replayed through the real risk engine;
    - ROB-005, noise injection;
    - ROB-008, the robustness report and score over seven R2 robustness gates;
    - `xq robustness simulate --truth genuine|overfit`, which records a known-truth simulated
      strategy as a run (synthetic, always exploratory);
    - `xq validate-strategy <run_id>`, the combined significance and robustness report with R1
      and R2 verdicts (`pass`, `fail` or `incomplete`; owner rules may make a criterion "not
      applicable", ADR 0058). It writes `reports/validation/` and the `stat_tests` and
      `robustness_results` tables. Its subject adapters cover `simulated_strategy` and
      `baseline_board` runs (`--strategy`, ADR 0059).
  - **Added in Sprint 13** (ADR 0060):
    - the model registry (`xq.registry`): model versions and content-hashed strategy bundles
      with statuses draft → candidate → validated → vault_passed → paper → live_eligible
      (`live` needs GATE-004), promoted only on a passing latest gate result (R1, R2, R3, R3,
      R4; `paper` only on an event-tier R3), enforced by the service and SQLite triggers (migrations
      0011–0016); an append-only
      performance history and an active-bundle pointer per environment with rollback;
      `xq registry register|list|show|promote|retire|history|activate|rollback|active`;
    - `xq gate evaluate <bundle>`: R1 and R2 from a validation of the bundle's origin strategy,
      a gate report with the plan's ten items, and a filled-in human review (GATE-003,
      `docs/specs/gate-review.md`);
    - `xq gate vault-token` and `xq gate vault-evaluate`: one vault token per validated bundle,
      ever; the one confirmatory vault evaluation of a rule bundle, checked before the first
      read, every read logged, R3 recorded (on the screening tier: it reaches `vault_passed`;
      `paper` needs an event-tier R3, ADR 0062, migration 0016).
  - **Added this session** (ADR 0063):
    - target kinds `realized_vol` (TGT-003), `excursion` (TGT-004), `triple_barrier` (TGT-005)
      and `derived_label` (TGT-006), with target sets `realized_vol.v1`, `excursions.v1`,
      `barriers.v1` and `derived.v1` in `config/targets.yaml`; `label_uniqueness` and
      `uniqueness_weights` (TGT-006) for in-fold sample weights;
    - the monthly retraining schedule (the research default when no `test_len` is given) and
      `stitch_oos` (WF-004);
    - the walk-forward report and `xq exp wf-report <run> --strategy <name>` (WF-005).
  - `xq exp reproduce <run_id>` rebuilds a baseline-board run's dataset, reruns it and reports
    REPRODUCED, NOT_REPRODUCED or RERUN_DIFFERENT_CODE.
  - Validation and robustness settings are in `config/validation.yaml`; the thresholds stay in
    `config/gates.yaml`.
- **Next:** the owner's review of this session (C-30). Sprint 7 part 2 (FEAT-001 … FEAT-006,
  FEAT-008), build-only on synthetic data, unless the owner orders otherwise. The owner downloads
  and ingests Dukascopy data with the CSV route (above); the C-8 session on the owner's Mac runs
  `xq validate` on it and prepares DQ-008.
  - After the quality review: H-0001 is registered with windows from the real data, alongside
    H-0000 (C-15, C-16); only then may the board run on real data. Whether rules may warm up on
    the 2014 data before `ds_base`'s start is C-29.
  - With real data, after the quality review (C-8): the research halves of Sprint 5 (C-16) and
    Sprint 6 (C-18), then the research parts of Sprints 7, 8 and 10.
  - Sprint 14 (paper trading) needs a venue and its data/broker API (an owner decision), and a
    bundle that passed R3 on the event tier (C-32); otherwise, by the plan's decision point,
    Sprints 14–16 validate infrastructure only, in a separate labelled environment such as
    `paper_infra`, never evidence (C-27 (1), approved).
  - GATE-004 records the SHA-256 of the signed GATE-003 review (C-31).
  - The real regime filter waits for REG-007 (Sprint 8).
- **Not allowed yet:**
  - running H-0001 (or any board) on real data before the owner has approved and registered it;
  - generating an EDA report, a statistical verdict report or a volatility board on real or
    pseudo-real data, or writing values to `config/horizons.yaml`, before the owner allows it
    (ADR 0035, Sprint 6 instruction);
  - promoting a volatility forecaster or replacing the interim sigma-hat (ADR 0044);
  - registering H-0000 before real data fixes its windows (ADR 0041);
  - running event backtests, reconciliations or reports on real or pseudo-real data (ADR 0048);
  - treating any event-tier result as evidence (synthetic quotes, placeholder costs, a
    provisional risk profile, no regime model);
  - treating the template strategy, the synthetic test forecaster or a simulated strategy as a
    candidate;
  - running the validation or robustness methods, or `xq validate-strategy`, on real or
    pseudo-real results, or citing their output as evidence, before a candidate exists and the
    owner allows it;
  - recording simulated strategies under a real hypothesis's family;
  - registering, gating or promoting a bundle on real data, issuing a vault token, or running a
    vault evaluation, before a candidate exists and the owner allows it (the vault is opened once
    per bundle, ever);
  - running anything on real Dukascopy data beyond the data pipeline and `xq validate` before the
    DQ-008 review (materializing the new target sets, or `xq exp wf-report` on a real run,
    included);
  - committing market data (it stays under the git-ignored `data/`);
  - treating Dukascopy's spreads as execution costs (no venue; costs stay placeholders,
    ADR 0057);
  - pushing to `main`.
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
| C-8 | Run `xq validate` on ≥ 1 year of real ticks, then the DQ-008 human review. The feed is now Dukascopy (ADR 0057): the owner runs `xq fetch dukascopy` and `xq ingest` (commands under "Owner's next step"); the next session runs clean, bars, spread statistics and `xq validate` where the data is | Sprint 2 | Owner (download, ingest), then Claude | open — the downloader and adapter are built; waiting on the owner's download |
| C-9 | Commit the approved `config/gates.yaml` (VAL-007) with its rationale in an ADR | Sprint 4 hold point | Claude | `72a8cdc` (ADR 0032) |
| C-10 | `1d` = one trading day (23 market hours); `4h` = 4 market hours | Sprint 4 hold point | Claude | `34dd006` |
| C-11 | No label and no entry for decisions taken while the market is closed; open decisions crossing a close keep their label (`crosses_close`) | Sprint 4 hold point | Claude | `d9aa756` |
| C-12 | Financing a cost on long and short while costs are placeholders; every net result marked "screening, placeholder costs" | Sprint 4 hold point | Claude | `71711e3` (cost model), `4a71cae` (the board prints it) |
| C-13 | CI job that builds the image's test stage and runs the suite in it | Sprint 4 hold point | Claude | `0b84ca7` |
| C-14 | Review the draft `experiments/hypotheses/H-0001.yaml` (the baseline board), then register it before any real-data run | Sprint 4 | Owner | reviewed at the start of Sprint 5: revision requested (ADR 0035), continued as C-15 |
| C-15 | H-0001 draft revised as the owner asked (ADR 0035), still **unregistered**: rule baselines over the full pre-vault history after each rule's warm-up, with the fold-aligned version stored for comparison; rules on 1d and 1h signal bars (not 15m); trial budget 36 (24 rule + 12 forecast-sign strategies); discovery and evaluation windows "set from the real data's depth at registration" (registration is refused until they are); descriptive slices by year and by session (reported, not tested). Owner: review the revision, including two readings of Claude's (lookbacks count bars of the signal timeframe; the fold-aligned version is not a separate trial) — both **approved** at the Sprint 5 review (ADR 0041). Remaining: Claude implements the revision in the board runner; H-0001 is registered with windows from the real data, alongside H-0000 | Sprint 5 start | Claude (board runner), then owner (registration with real windows) | Claude's part closed: the board runner implements the revision (ADR 0061, C-15 commit on `claude/elegant-cannon-rbagtp`); open — the owner registers H-0001 with windows from the real data |
| C-16 | Research half of Sprint 5, after real data (C-8): fix `eda.discovery.end` from the data's depth; pre-register the standing descriptive hypothesis H-0000 (zero trial budget, family `descriptive`; the EXP-002 schema accepts it since `89f241a`, ADR 0042) alongside H-0001, and run EDA under it (ADR 0041); run the EDA confirmatory; review it; write `config/horizons.yaml` with `xq research admit-horizons`; write `docs/research/hypotheses-backlog.md` and pre-register its top items | Sprint 5 (build-only) | Owner (data, window, approval), then Claude | open — schema prerequisite done; blocked on C-8 and the owner's go-ahead |
| C-17 | DATA-013 secondary long-history adapter: build only if the owner decides a secondary feed is needed (depends on the broker's history depth) | Sprint 5 start | Owner (decision) | decided (ADR 0057): Dukascopy is the primary feed. Built: `89a5b67` (adapter), `ca8f1ec` (`xq fetch dukascopy`), `9dd2f17` (default source), `edec7e5` (CSV timestamp guard) |
| C-18 | Research half of Sprint 6, after real data (C-8) and the owner's go-ahead: pre-register the statistical and volatility studies (families and trial budgets); run STAT-001/002/003 on the discovery window and STAT-006 in walk-forward at the admitted horizons (needs `config/horizons.yaml`, C-16) and write the verdict report; run the volatility board on real 1m/5m bars (daily and hourly periods) on identical folds; apply `select_forecaster`; the owner decides whether the selected forecaster replaces the interim sigma-hat (an ADR and a configuration change); add a CLI for these reports; measure their speed on real data. Trial rules approved (ADR 0046): STAT-001 … STAT-003 record none (descriptive, under H-0000); STAT-006 and the volatility board record one per (model, horizon) in the `linear_forecasts` and `volatility_models` families. If STAT-002 or STAT-003 finds dependence in returns, write and pre-register H-0002 (linear predictability, with `ar1`) | Sprint 6 (build-only) | Owner (data, go-ahead, promotion), then Claude | open — blocked on C-8 and C-16 |
| C-19 | Whether the `ar1` forecast baseline (BASE-003) joins H-0001's board, which raises its approved trial budget from 36 to 40, or is evaluated under its own pre-registered hypothesis | Sprint 6 (ADR 0045) | Owner (decision) | decided (ADR 0046): `ar1` stays off H-0001 (budget 36), stays on benchmark boards, and gets H-0002 only if STAT-002/003 find dependence on real data (C-18) |
| C-20 | Owner review of Sprint 11's open points (ADR 0049): the weekly-close blackout length (60 min) and weekend-exit lead (30 min), provisional; the reconciliation tolerance applied to the raw equity difference, sizing included (at 100,000 USD, lot-step rounding alone can exceed 5 % of costs; reported separately); limit orders never filling better than their price; the provisional margin rate 0.05; Sprint 12's scope under ADR 0048 | Sprint 11 | Owner, then Claude | decided (ADR 0050, ADR 0051): tolerance after the sizing effect; 60-min blackout, optional weekend exit (off) and 5 % margin approved; limit orders fill only on a trade through by ≥ 1 tick, never better; Sprint 12's data-independent part now, then Sprint 9. Implemented: `c4c490b` (tolerance after sizing), `d835141` (limit trade-through) |
| C-21 | TGT-002 forward-return labels take the first quote at or after the intended fill time even when the market is closed (a stray quote in the daily break within the fill delay), the rule the screener no longer follows (`aa88b2a`). Fixing it bumps the target code version and changes dataset hashes, so it waits for the owner's go-ahead | Sprint 11 | Owner (go-ahead), then Claude | `fdd5261` (ADR 0050): market-hours fills only, forward-return code version 5 |
| C-22 | Owner review of Sprint 12 A's open points (ADR 0052): the provisional risk profile `risk-1` (everything but the owner's 0.5 % per trade and 15 % drawdown halt); refused intents that still close an opposite position (`risk rule:` exits); stops required on every long or short intent, widened when closer than 3 spreads and refused beyond 5 daily sigma-hats; the kill switch ignored by backtests unless given; **probability scaling on the raw calibrated p (0.5 → 0.6) suits 1:1 payoffs only — for 2:1 barriers break-even is p = 1/3, so either each strategy's profile matches its payoff or scaling moves to the edge p − SL/(TP + SL)**; the spread filter's hour-of-week median in New York time with an overall-median fallback | Sprint 12 A | Owner, then Claude | decided (ADR 0053): sizing scales on the edge per unit of risk, `clip(ev_r / ev_r_full, 0, 1)` with `ev_r = p_lcb x TP/SL - (1 - p_lcb) - cost/SL` and p_lcb the lower confidence bound; the other points approved as they stand (provisional profile values; a refused reversal still closes the opposite position; backtests may run without a kill switch). Implemented: the C-22 commit of Sprint 9 |
| C-23 | PAPER-001 requirement: the paper and live runtimes refuse to start without a kill-switch source (a file, an environment variable or the database flag); the backtester may run without one | Sprint 12 A review (ADR 0053) | Claude, when PAPER-001 is built | open |
| C-24 | Owner review of Sprint 9's open points (ADR 0054). (1) SPA and the Reality Check over-reject under strong serial dependence in short samples (AR(1) φ = 0.4, 400 periods: 15 % and 20 % at a 10 % level); test such families on non-overlapping periods. (2) The ±20 % neighbourhood gate reads the joint neighbourhood (3^k − 1 points), stricter than one parameter at a time. (3) Cost stress runs the plan's grid one dimension at a time plus the gate's joint scenario (no full factorial); a financing credit is divided by the multiplier. (4) Robustness drawdowns start from the capital; BT-003's `drawdown_metrics` does not (known issue, fix proposed as a separate task). (5) Volatility terciles are cut on the whole sliced period (descriptive only); slice names are checked when loaded, not at registration; the template's `volatility regime` became `volatility tercile`. (6) The execution delay shifts entries and exits alike. (7) `xq exp reproduce`: a reproduction adds no trials, the DSR is shown but not judged, provenance differences are reported rather than refused, and only `baseline_board` runs have a reproducer. (8) `xq validate-strategy` is not built: no Sprint 9 task covers it; proposed with ROB-008 and GATE-001 | Sprint 9 | Owner, then Claude | decided (ADR 0055): (1) SPA/Reality Check on the gates' bootstrap convention, size re-run (still above 1.5x nominal: 18.5 % SPA, 15.4 % Reality Check at 10 %), so a per-sample size check puts "test over-rejects on this sample" on the gate result; (2) BT-003 drawdowns start from the capital; (3) the neighbourhood gate on the full ±20 % grid (seeded sample of 243 points above 3^5), a one-at-a-time table in the report, a ridge optimum fails; (4) slice names validated at registration, volatility terciles labelled "descriptive, cut ex post"; (5) a reproduction is REPRODUCED only with the same git sha, config hash and lock hash and metrics within tolerance, otherwise RERUN_DIFFERENT_CODE; approved as they stand: one-dimension cost stress plus the combined scenario, financing credit divided by the multiplier, equal entry and exit delay, reproductions add no trials, DSR shown not judged. Implemented: (1) `468b2f0`, (2) `6ca4562`, (3) `acb47b1`, (4) `b22a50d`, (5) `cf12d15`; Claude's readings are carried into C-25 |
| C-25 | Owner review of Sprint 12 B's open points (ADR 0055, ADR 0056). (1) The SPA/RC over-rejection warning is decided per sample by a size check (AR sieve null families, 500 by default), not attached to every result: on iid-like samples the tests are near nominal; its Monte Carlo error is about 1.5 points at 500 families. (2) A reproduction with the same code but a metric out of tolerance is a third status, NOT_REPRODUCED (not "different code"); never counted as reproduced. (3) PBO judges the choice among configurations: a genuine edge in a homogeneous family (near-identical configurations) has PBO near 0.5 and fails R2's `pbo_max`; mirror-image configurations break the DSR's benchmark instead; gates unchanged. (4) A gate that cannot be evaluated makes a verdict `incomplete`, never `pass`: a strategy without tunable parameters has no neighbourhood and never reaches R2 `pass`. (5) A validation run records no trials (it selects nothing; a variant picked from its diagnostics needs a new run). (6) The Monte Carlo keeps R-multiples as observed (a loss beyond the stop keeps its size) and calls a path ruined at half the capital (provisional). (7) `xq validate-strategy` has a subject adapter only for simulated runs; the baseline-board adapter (rebuilding the board's screening context) is the next build task. (8) Volatility-tercile slices bucket the sigma-hat warm-up days as `no_sigma_hat` | Sprint 12 B | Owner, then Claude | decided (ADR 0058): (1) PBO not applicable, and R2 `pbo_max` N/A, when the family has at most 2 effective trials (DSR still applies); (2) trial clustering on absolute correlation (\|ρ\| ≥ 0.7, ≥ 60 common days), mirror images one cluster; (3) SPA/Reality Check gated on a size-adjusted p-value from the sample's simulated null when the size check flags over-rejection (threshold 0.10 unchanged), both p-values reported; (4) the neighbourhood gate N/A only when the hypothesis declares `parameters_fixed_a_priori: true` with a `source`, otherwise every numeric constant of the strategy's config is perturbed; approved as they stand: NOT_REPRODUCED, no trials for validation runs, ruin at 50 % (provisional) with gap losses at observed size; (7) the baseline-board adapter is the next build task. Implemented: (1) `e0e48bc`, (2) `d27e16e`, (3) `92d5e20`, (4) `d38cfb5`; (7) `574ca95` (ADR 0059) |
| C-26 | Owner review of the Dukascopy session's open points (ADR 0057). (1) The `.bi5` endpoint has been reported to time out since 7 July 2026 (dukascopy-node issue #254) and could not be tested: if it is still down, keep the dukascopy-node CSV route, or have Claude add Dukascopy's JSON API to `xq fetch` (hourly JSON files, same guarantees). (2) Files are stored as the vendor's bytes, one per hour, named after the UTC hour; empty hours get no file. (3) Canonical tick sizes stay NaN (volume units undocumented); volumes are kept in the raw mirror. (4) Download pace: one request at a time, 0.5 s apart, 4 attempts with backoff, empty market hours asked twice and recorded only once a later hour has ticks, 24 in a row stop the run. (5) The calendar is unchanged until `xq validate` on real data shows Dukascopy's hours (Dukascopy's hours pages were blocked here); the table in ADR 0057 lists the checks. (6) `ds_base.yaml` keeps its ~4-year window (start 2021-09-26) although Dukascopy goes back to 2003: a longer window is the owner's decision, before results. (7) `--source` now defaults to `data.primary_source` in the pipeline commands | Dukascopy session | Owner, then Claude | decided (ADR 0062): (1) the dukascopy-node CSV route is the working route (the `.bi5` endpoint answered HTTP 503 on 2026-10-04; March 2024 ran end to end), corrected command `-r 3 -re -fr`, the owner's monthly loop in `docs/runbooks/real-data.md`, no JSON API; (2), (3), (4), (5), (7) approved; (6) `ds_base.yaml` from 2015-01-01 (download from 2014-01-01), fixed before any result. Implemented: `fcfa9e1` (README, runbook), `d8da183` (`ds_base` start) |
| C-27 | Owner review of Sprint 13's open points (ADR 0060). (1) Under the registry's rules a bundle reaches paper only through R1, R2 and R3; the plan's decision point (no bundle passes R2) wants a baseline bundle on the paper infrastructure, which would need a separate, labelled environment (for example `paper_infra`, never evidence) — not built. (2) A failed vault evaluation spends the bundle's one vault access; any exception needs an ADR. (3) R3's risk-limit breaches are read on the screening tier (daily losses against the risk profile's limits), not from the risk engine's refusals, until a candidate runs on the event tier. (4) Enforcement is in the code and SQLite triggers; an administrator with write access to the database file can bypass them (every gate result names its evaluator, evidence and policy hash); the migrations refuse a database without the triggers until PAPER-004 writes PostgreSQL's. (5) Promotion to `paper` reads the same R3 result as `vault_passed`. (6) A signed GATE-003 review is a document, not a database record; whether it must be recorded is GATE-004's question. (7) Only rule bundles can be bundled and vault-evaluated (models wait for ML-009); a forecast-sign board strategy's neighbourhood is not evaluated | Sprint 13 | Owner, then Claude | decided (ADR 0062): (1), (2), (4), (5), (7) approved; (3) approved with a new requirement — promotion to `paper` needs R3 recomputed on the event tier with the real risk engine (enforced in the transition rule and the database); (6) GATE-004 records the SHA-256 of the signed GATE-003 review. Implemented: (3) `0704849` (the rule; the evaluator is C-32); (1) with Sprint 14 if needed; (6) is C-31 |
| C-28 | Owner review of the C-15 session's readings (ADR 0061). (1) The H-0001 board test (Sharpe p-value, DSR, random-entry null, slices) uses each rule's full history after its warm-up, while `xq validate-strategy` (R1, R2), the registry's `backtest` history (MREG-004) and the vault's walk-forward interval (R3) keep judging a rule on its fold-aligned record (identical days for every strategy and every later candidate; conservative). Confirm, or move validation and the registry to the full history for rules. (2) The first evaluation day counts from the first evaluation decision (the rule is flat before it). (3) Donchian now enters only once its exit channel is known (only `exit > max(entry, atr_window)` changes; no board has one) | C-15 session | Owner | decided (ADR 0062): all three readings confirmed as built; no code change |
| C-29 | Whether the board's rule warm-ups may read signal bars before the dataset's start. With `ds_base` from 2015-01-01 the owner downloads from 2014-01-01 for warm-up, but under ADR 0061 a rule warms up on the dataset's own signal bars: the 2014 data serves the dataset's 10-day `warmup`, sigma-hat and the quality report, not the rules (`tsmom_252@1d` is first evaluated about a year after the start). Using the 2014 bars needs a change to the board runner and its ADR | ADR 0062 (C-26 (6)) | Owner | open |
| C-30 | Owner review of Sprint 7 part 1's readings (ADR 0063). (1) TGT-003's realized volatility includes the overnight return when its window crosses a close (VOL-002's realized measures leave it out). (2) The barrier multiples are 1.0 and 1.0 sigma-hats over the horizon, provisional, per target set; other payoffs get other set versions. (3) The trade/no-trade label mirrors the placeholder cost model without its session multipliers (rollover ×3, US releases ×2), so it understates slippage in those windows; "expected net P&L" is read as the trade's realized return net of costs at their placeholder values. (4) Uniqueness weights are computed in-fold on purged training labels (ML-002), not stored in `targets.parquet`; over labels reaching a later window, purging must use `weight_end`. (5) `xq exp wf-report` reads a recorded run's stored returns and folds, records no run and no trial, and its decay p-value is descriptive (R2 is judged only by `xq validate-strategy`) | Sprint 7 part 1 | Owner | open |
| C-31 | GATE-004 requirement (owner's decision C-27 (6), ADR 0062): the live-readiness record stores the SHA-256 of the signed GATE-003 review document, so the reviewed text is identified | ADR 0062 | Claude, when GATE-004 is built | open |
| C-32 | The event-tier R3 evaluation (owner's requirement C-27 (3), ADR 0062): R3 recomputed with the event backtester and the real risk engine, recorded with `evidence_tier: event`. The transition rule is enforced (migration 0016); the evaluator is not built, so no subject can reach `paper` | ADR 0062 | Claude, when a candidate runs on the event tier | open |

## Open owner decisions

| Question | Options | Default in use |
| --- | --- | --- |
| Execution venue | none for now (data-only project, ADR 0057); OANDA v20 is the candidate live feed; MT5 or cTrader brokers | no venue; costs stay placeholders; `mt5_primary` kept as an optional source (ADR 0004) |
| Discovery window (EDA-001) | the first 50–60 % of non-vault data (plan); a fixed end date | first 50 % of the span from the dataset's start to `vault.start`, at a trading-day start (with `ds_base` from 2015-01-01: the start of trading day 2020-05-15, ADR 0062); to be fixed as `eda.discovery.end` from the real data's depth (C-16) |
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
choices, made by Claude within the plan and the owner's instructions, are in ADR 0054. Decided
at the Sprint 9 review (ADR 0055): SPA and the Reality Check on the gates' bootstrap convention
with a per-sample over-rejection warning, BT-003 drawdowns from the capital, the full ±20 %
neighbourhood grid, slice names checked at registration with volatility terciles "descriptive,
cut ex post", and REPRODUCED only on the same code; one-dimension cost stress with the combined
scenario, the financing credit divided by the multiplier, equal entry and exit delay, no trials
for reproductions and the DSR shown not judged approved as they stand. Sprint 12 B's own choices
(ADR 0055, ADR 0056) were decided at its review (ADR 0058, C-25): PBO not applicable without a
meaningful selection, trial clustering on absolute correlation, SPA gated on a size-adjusted
p-value when it over-rejects, and a neighbourhood not applicable only by an a-priori declaration
with a source; NOT_REPRODUCED, no trials for validation runs and ruin at 50 % approved as they
stand. Sprint 13's own choices (ADR 0059, ADR 0060) and the C-15 session's readings (ADR 0061)
were reviewed in C-27 and C-28. Decided at the review of the Dukascopy session, Sprint 13 and
C-15 (ADR 0062, 2026-10-04): the dukascopy-node CSV route is the working
route (corrected flags `-r 3 -re -fr`, the owner's monthly loop in `docs/runbooks/real-data.md`),
`ds_base.yaml` starts on 2015-01-01 (data from 2014-01-01 for warm-up), the C-15 readings are
confirmed, promotion to `paper` needs R3 recomputed on the event tier with the real risk engine,
GATE-004 records the SHA-256 of the signed GATE-003 review, third-party loggers log at WARNING,
and the other open points of C-26 and C-27 are approved as they stand. Decided on 2026-09-28
(ADR 0057): the project is data-only for now, with no execution venue; Dukascopy's XAUUSD bid/ask
ticks are the primary research feed (superseding ADR 0004's choice), `mt5_primary` stays optional,
OANDA v20 S5 candles are a later cross-check (DATA-011) and candidate live feed, and costs stay
placeholders until a venue exists. Claude's choices within it were reviewed in C-26 (ADR 0062).

## Provisional assumptions not yet confirmed

| Assumption | Value in use | Where | Confirmed by |
| --- | --- | --- | --- |
| Execution venue | none (data-only project); the MT5 broker of the optional `mt5_primary` is unnamed | ADR 0057, `config/base.yaml` | the owner choosing a venue |
| Source clocks | `dukascopy`: `UTC` (the vendor's hour files are UTC hours); optional `mt5_primary`: `NY+7` (UTC+2/+3, US DST dates) | ADR 0003, ADR 0004, ADR 0057 | DQ-004 gap locations on real Dukascopy data |
| Dukascopy file format | `.bi5` = LZMA "alone" stream of 20-byte big-endian records (ms offset, ask, bid, ask volume, bid volume), XAUUSD points / 1000, URL months from 00, first tick 2003-05-05; CSV = dukascopy-node's `timestamp,askPrice,bidPrice,askVolume,bidVolume` | `xq.data.adapters.dukascopy`, `config/base.yaml`, ADR 0057 (from dukascopy-node's source; the vendor was unreachable) | the first real download and ingest (plausible prices, no refused files) |
| Download pace | one request at a time, ≥ 0.5 s apart, 30 s timeout, 4 attempts (backoff 2, 4, 8 s), empty market hours asked again after 5 s and recorded only once confirmed, 24 in a row stop the run | `sources.dukascopy.download`, ADR 0057 | the owner's first download |
| Contract terms | tick 0.01, 100 oz per lot, lot step 0.01, max 100 | `config/instruments/xauusd.yaml` | broker contract spec |
| Trading calendar | 18:00–17:00 New York, NYSE holidays, 13:30 early closes, one schedule for every year | ADR 0002, `config/sessions.yaml`; re-checked against Dukascopy in ADR 0057 (its hours pages were unreachable) | DQ-004 on real Dukascopy data, read per year, then DQ-008 |
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
| Trial clustering | \|ρ\| 0.7 (absolute correlation, C-25), 60 common trading days; frozen with the gates (ADR 0032); Sharpe variance across clusters (ADR 0058) | `config/base.yaml` `experiments` | fixed before results |
| Sigma-hat | interim EWMA, span 96 base bars | `fwd_returns.v1` | a VOL-006 selection on real data, approved by the owner and recorded in an ADR (C-18) |
| Sprint 7 targets (TGT-003 … TGT-006) | the forward returns' windows (latency 1 s, fill delay 300 s, sigma-hat span 96); realized volatility on a 5-minute market-time grid of the mid; barriers at 1.0 / 1.0 sigma-hats over the horizon, every tick a path point; big move above 1.0 sigma-hat; trade/no-trade costs mirroring the placeholder cost model without session multipliers | `config/targets.yaml`, ADR 0063 | fixed before results; changes need a new set version and an ADR |
| Retraining schedule | monthly (from the start of the trading day dated the 1st) when no `test_len` is given; the board keeps its fixed 91-day folds | `xq.validation.splitters`, ADR 0063 | fixed before results |
| Baseline board | fixed parameters (rules on 1d and 1h signal bars, lookbacks in bars of the signal timeframe, MA 20/50 and 50/200, 10 % vol target, 1,000 random-entry seeds); rules evaluated over the full pre-vault history after their warm-up, forecast-sign strategies on the folds; folds: expanding, ≥ 3 years training, 91-day tests, 1-day embargo | `experiments/configs/baselines/board.yaml`, ADR 0033, ADR 0034, ADR 0035, ADR 0061 | fixed before results; changes need an ADR |
| Random-walk forecast baseline | persistence of the latest completed bar return of the horizon's timeframe (`zero_return` covers the price random walk) | ADR 0033 | owner review of Sprint 4 |
| EDA parameters | bootstrap 1,000 resamples, block ≥ 5 trading days of bars and ≤ n/10; Hill tails 5 %; ≥ 20 lags (one trading day); Bonferroni family-wise 0.05 with cluster-robust Student-t intervals; LBMA windows −5/+30 min; VR q = 2, 4, 16, 92 on 15m; runs on 1h | `config/eda.yaml`, ADR 0038 | fixed before results; changes need an ADR |
| Statistical tests (STAT-001 … STAT-006) | level 0.05; ADF with AIC lags, KPSS level and trend, Zivot-Andrews 15 % trimming; Ljung-Box lags 1, 5, 10, 20 with Holm across lags; ARCH-LM lags 5, 10; variance ratios at 2 … 64 bars with Chow-Denning; ARMA models `ar1`, `arma11`, `ar_aic` (p ≤ 5 by AIC on training folds) against `zero_return` and `random_walk`, DM with Holm across horizons | `config/stats.yaml`, ADR 0043 | fixed before results; changes need an ADR |
| Volatility research (VOL-001 … VOL-006) | estimator window 20 bars, Wilder ATR 14; RV from 1m and 5m returns per hour and trading day; diurnal factor day-standardized, at least 20 training rows per bucket; benchmarks `rolling_22`, `ewma_0.94`, `ewma_0.97`, HAR (1, 5, 22 days; 1, 23, 115 hours), HAR floor 1 % of mean training RV; GARCH, GJR, EGARCH × normal, t, skewed t, zero mean, 1,000 EGARCH simulations; QLIKE primary, MCS 90 % (1,000 resamples, block 5), DM level 0.05 against HAR; selection default `ewma_0.94`, challengers' one-sided DM p-values Holm-adjusted before the 0.05 level (ADR 0046) | `config/volatility.yaml`, ADR 0044 | fixed before results; changes need an ADR |
| Horizon admission | cost-to-volatility bound 0.3 (plan default) on the overall mean ratio; horizons are TGT-002 labels 1m–1d measured from 1m bars on a 5-minute decision grid with `fwd_returns.v1`'s latency and fill delay; spread at the fills; slippage sigma-hat from the last 60 one-minute returns (causal fallback); financing at the mean of the long and short rates | `config/eda.yaml`, ADR 0037, ADR 0040 | owner review of the first real EDA; broker costs |
| Validation and robustness procedures | SPA size check: 500 null families × 199 resamples, AR sieve ≤ 5, warn above 1.5 × level (owner); perturbation levels 10/20/30 %, full grid up to 243 points (owner); Monte Carlo 1,000 paths, one R = 3 daily sigma-hats, ruin at 50 % of capital (provisional); noise at 0.25–5 spreads and 0.1–1 feature sigma, 20 draws; PBO 16 blocks; bootstrap convention and thresholds from `config/gates.yaml` | `config/validation.yaml`, ADR 0055, ADR 0056 | fixed before results; changes need an ADR |
| Known-truth simulated strategies | 20 years of daily bars; costs half of a 1.5 bp spread + 0.5 bp slippage + 0.35 bp commission per turnover, 0.15 bp a day financing; genuine: AR(1) drift (persistence 0.99, 6 bp), 24 trend configurations (lookbacks 2–80, deadbands 0.25/0.5/1); overfit: 50 independent-noise configurations; baseline buy-and-hold | `xq.robustness.simulated`, ADR 0056 | synthetic only; never evidence |

## Known issues and technical debt

- No real market data exists. Sprints 2–6 and 11 are tested only on synthetic data (Sprints 5 and
  6 also on simulated processes); nothing is validated on real data (C-8).
- The Dukascopy `.bi5` endpoint (`datafeed.dukascopy.com`) answered HTTP 503 on the owner's
  machine on 2026-10-04 (reported to time out since 7 July 2026). The dukascopy-node CSV route
  is the working route (ADR 0062). `xq fetch dukascopy` is kept but tested only against a
  scripted vendor and a local HTTP server.
- The calendar was not compared with Dukascopy's published hours or trading-breaks calendar: the
  domain is blocked here. `xq validate` on real data settles it (ADR 0057, decision 4).
- Dukascopy tick volumes are kept in the raw mirror only; canonical tick sizes are NaN (units
  undocumented). Ingesting both Dukascopy formats for the same period would duplicate ticks (the
  second copy is flagged `DUP_EXACT`).
- The board screens every rule over every decision of the dataset (ADR 0061): 24 rule
  strategies, each with 1,000 random-entry screens over its full history. Its speed and memory on
  four years of real ticks are unmeasured. Board runs recorded before C-15 carry the old
  `signal_timeframe` key and are refused by the new board configuration (only synthetic test runs
  exist).
- Sprint 7 part 1 is tested on synthetic data only (ADR 0063). No dataset spec names the new
  target sets yet. Their speed on four years of real ticks is unmeasured: the barrier kind loops
  over decisions in Python, and the realized-volatility grid holds up to 276 points per decision
  for 1d. The trade/no-trade label omits the cost model's session multipliers (C-30 (3)).
  Uniqueness weights are a library function: nothing applies them in-fold until ML-002.
- `WalkForwardConfig` now dumps a `schedule` field, so board and walk-forward trial configs
  recorded before this session hash differently from new ones (only synthetic test runs exist).
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
- Vault gate tokens are issued only by GATE-002, one per validated bundle (ADR 0060).
  `xq validate --include-vault` keeps its explicit confirmation flag: grading vault days is a
  pipeline stage, and the vault evaluation refuses days a quality run has not graded.
- The registry's triggers are SQLite's: migrations 0011–0016 refuse another database until
  PostgreSQL's versions exist (PAPER-004). An administrator with write access to the database
  file can bypass them; every gate result names its evaluator, evidence and policy hash.
- Only rule bundles from a board run can be bundled and vault-evaluated; bundles of forecast
  models wait for registered model versions (ML-009). R3's risk-limit breaches are read on the
  screening tier (daily losses against the risk profile), not from the risk engine.
- Parquet bytes depend on the pyarrow version, so a lockfile change can change dataset hashes
  without changing values (ADR 0017).
- Target computation reads a month of ticks at a time; memory and speed on four years of real
  ticks are unmeasured. The same holds for the baseline board (24 strategies and 24,000
  random-entry screens by default), although it reduces quotes to those the screener reads.
- The validation and robustness methods (VAL-001 … VAL-006, ROB-001 … ROB-008, the R1
  best-baseline test and the R2 decay test) are wired into `xq validate-strategy`. They are proven
  on simulated strategies only, and none has run on real data.
  - `simulated_strategy` and `baseline_board` runs can be validated (ADR 0059). A board
    strategy is rebuilt from the run and refused unless its returns equal the recorded ones. A
    forecast-sign strategy's neighbourhood is not evaluated (its model is not refitted), so its
    R2 verdict is incomplete unless its parameters are declared fixed a priori. The speed of a
    board validation on real ticks is unmeasured (several hundred re-screens).
  - `xq gate evaluate` (GATE-001) records R1 and R2 from these validations; a signed human
    review (GATE-003) is a document, not a database record.
- A validation with the default settings takes a few minutes on 20 years of daily data. The
  Monte Carlo replays about 0.3 ms per trade through the pure-Python risk engine, and the SPA size
  check simulates 500 null families.
- SPA and the Reality Check over-reject under strong serial dependence in short samples, even on
  the gates' Politis–White block (AR(1) φ = 0.4, 400 periods, 8 strategies: 18.5 % and 15.4 % at a
  10 % level; more with more strategies). Every SPA gate result therefore runs a per-sample size
  check and carries "test over-rejects on this sample" when the simulated size exceeds 1.5 times
  the level (ADR 0055). When it does, the gate reads the size-adjusted p-value from that sample's
  simulated null at the unchanged threshold (ADR 0058), which restores the nominal level on those
  samples (9.0 % SPA, 10.0 % Reality Check at 10 %). The null p-values use the size check's 199
  resamples per family, coarser than the observed test's 10,000.
- Re-running the board with `xq baselines run` records its trials again (every evaluation counts),
  so the raw trial count grows with re-runs; the review flag fires when raw / effective exceeds
  10. `xq exp reproduce` does not: a reproduction's configurations are the original's trials.
- `xq exp reproduce` has a reproducer only for `baseline_board` runs; other kinds are refused by
  name. A reproduction counts only with status REPRODUCED (same git sha, config hash and lock hash,
  metrics within tolerance; ADR 0055).
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
- Not started, deferred by plan: DQ-005 (feed consistency), DATA-011 (with OANDA v20, ADR 0057),
  DATA-012, BASE-004, EDA-007, STAT-004, STAT-005 (Sprint 8), STAT-007 (gated); SARIMA (only if
  EDA-004 finds a stable daily cycle) and FIGARCH (only if STAT-004 finds long memory) are not
  built.
- The raw-file permission test is skipped when the suite runs as root (it runs in CI, and in the
  Docker test stage, which runs as the non-root user).

## Status per phase

| Phase | Tasks done | Implemented | Tested (synthetic) | Validated on real data |
| --- | --- | --- | --- | --- |
| 0 Architecture | ARCH-001 … ARCH-008 | yes | yes (image verified in CI) | not applicable |
| 1 Market data | DATA-001 … DATA-010, DATA-013 (Dukascopy adapter and `xq fetch dukascopy`, the primary feed) | yes | yes (synthetic `.bi5` and CSV fixtures, a scripted vendor, a local HTTP server) | no |
| 2 Data quality | DQ-001 … DQ-004, DQ-006, DQ-007 (DQ-005, DQ-008 open) | yes | yes | no |
| 3 Datasets | DS-001 … DS-007 | yes | yes | no |
| 4 Exploratory research | EDA-001 … EDA-006 (EDA-007 in Sprint 8) | yes | yes (synthetic data, simulated processes) | no (no report on real data yet, C-16) |
| 5 Statistical time series | STAT-001, STAT-002, STAT-003, STAT-006, STAT-008 framework (STAT-004, STAT-005 in Sprint 8; STAT-007 gated) | yes | yes (simulated processes, recovery registry) | no (no verdict report on real data, C-18) |
| 6 Volatility research | VOL-001 … VOL-006 | yes | yes (simulated processes, recovery registry) | no (no volatility board on real data; nothing promoted, C-18) |
| 9 Targets | TGT-001 … TGT-006 | yes | yes (hand-computed cases, known barrier hit times, the uniqueness hand example, leakage harness) | no |
| 10 Baselines | BASE-001, BASE-002, BASE-003, BASE-005, BASE-006 (BASE-004 later) | yes | yes | no |
| 12 Walk-forward | WF-001 … WF-006 | yes | yes (monthly schedule, stitched series without overlaps or gaps, planted decay found, report on a synthetic board run) | no |
| 13 Backtesting | BT-001 … BT-010 | yes | yes (hand-computed trades, simulated quotes, reconciliation of the tiers) | no |
| 14 Risk engine | RISK-001 … RISK-006 | yes | yes (hand-computed cases, exact thresholds, property tests, architectural test, synthetic runs) | no |
| 15 Signal engine | SIGNAL-001 … SIGNAL-005 (the regime filter a pass-through until REG-007) | yes | yes (golden EV, filter cases, forecast-to-fill with a synthetic stub forecaster) | no |
| 16 Robustness research | ROB-001 … ROB-008 (report and score; Monte Carlo through the real risk engine; noise injection) | yes | yes (simulated strategies with known truth: overfit vs genuine edges, planted edges, coverage, a ridge, reckless risk profiles; recovery registry) | no |
| 17 Statistical validation | VAL-001 … VAL-007; `xq validate-strategy` (R1 best-baseline test, R2 decay test, per-sample SPA size check) | yes | yes (published examples, independent implementations; simulated noise, graded and overfit families; a recorded genuine edge passes R1 and R2, a recorded overfit strategy fails R2) | no |
| 18 Experiment tracking | EXP-001 … EXP-006 (reproduction status, ADR 0055); reserved trial families (ADR 0047); `backtests` (migration 0009), `stat_tests` and `robustness_results` (migration 0010) | yes | yes (a fixture board run reproduces; other code is RERUN_DIFFERENT_CODE) | not applicable until real research runs |
| 19 Model registry | MREG-001 … MREG-005 (migrations 0011–0016, ADR 0060, ADR 0062) | yes | yes (every status pair against the database's trigger, rollback to the exact hash, tamper refusal, CLI, a board rule bundle registered and gated end to end) | not applicable until a candidate exists |
| 25 Release gate | GATE-001 … GATE-003 (GATE-004 later) | yes | yes (gate evaluation and the one-time vault procedure end to end on synthetic ticks with a test vault start; second vault access refused) | no (the vault has never been opened) |
| All other phases | not started | no | no | no |
