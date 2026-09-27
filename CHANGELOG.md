# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries reference backlog task
IDs from `docs/specs/development-plan.md`.

## [Unreleased]

### Added

- ARCH-001: repository scaffold — `src/xq` package managed by uv (Python 3.12), README, this
  changelog, `CLAUDE.md` with the binding invariants, ADR 0001, and the development plan in
  `docs/specs/development-plan.md`.
- ARCH-002: quality tooling — ruff lint and format, mypy in strict mode over `src/`, pre-commit
  (whitespace hygiene, gitleaks, ruff and mypy from the locked environment), pytest markers
  `unit`, `integration`, `property`, `leakage`, `slow`, `research` applied automatically by
  directory, warnings treated as errors.
- ARCH-003: layered configuration (`xq.core.config`) — `config/base.yaml` → profile YAML
  (`dev`, `research`, `paper`, `prod`) → `XQ_` environment variables → explicit overrides,
  validated into a frozen pydantic-settings `AppConfig`; `config_hash()` over canonical JSON
  (secrets excluded); secrets typed `SecretStr` and accepted only from `XQ_SECRETS__*` variables;
  vault start fixed at the start of trading day 2025-09-26 (2025-09-25T21:00:00Z).
- ARCH-004: structured logging (`xq.core.logging`) — structlog JSON lines to stderr and to
  `logs/xq.jsonl`; standard-library loggers share the same pipeline; `run_id`, `git_sha` and
  `config_hash` are bound to every line.
- ARCH-005: core utilities — `Timeframe` (durations, trading-day anchoring for 4h/1d), `Side`
  (buy fills at ask, sell at bid), `PriceBasis`; UTC helpers that reject naive timestamps;
  `trading_day` / `trading_days` / `trading_day_bounds` with the 17:00 New York roll; ULID and git
  identifiers; `set_global_seed`, `make_rng` and SHA-256-based `derive_seed`.
- ARCH-006: `xq` CLI (typer) — `xq --version`, `xq config show [--format yaml|json]` with secrets
  masked and the config hash, global `--profile`, `--set section.key=value` and `--config-dir`
  options, and placeholder command groups (`dataset`, `exp`, `baselines`, `research`,
  `robustness`, `registry`, `gate`) for later sprints.
- ARCH-007: GitHub Actions CI (`.github/workflows/ci.yml`) — locked `uv sync`, ruff lint and
  format, mypy, and pytest (everything except `slow` and `research`) with coverage, all under
  `TZ=Asia/Tokyo` to catch hidden local-time dependencies.
- ARCH-008: development Docker image (`docker/Dockerfile`: python 3.12-slim, uv from PyPI, locked
  dependencies, non-root user), `docker-compose.yml` with the `research` profile, `.env.example`.
- DATA-001: instrument specification — `config/instruments/xauusd.yaml` (tick 0.01, 100 oz per
  lot, lot step/min 0.01, max 100, 17:00 New York rollover; values to be confirmed against the
  chosen broker), loaded as `AppConfig.instruments` from one YAML per instrument, with
  per-venue overrides and exact `Decimal` lot maths (`round_lots` always rounds down).
- DATA-002: trading calendar and sessions — `config/sessions.yaml` (`AppConfig.sessions`),
  `xq.data.calendar.MarketCalendar` (NYSE and English holiday calendars, full closes, early
  closes, Sunday open) and `xq.data.sessions.build_session_table`, which returns one row per
  trading day with market hours, Tokyo/London/New York sessions, the London–New York overlap and
  LBMA, COMEX, US-data and rollover anchors, all in UTC. Local times must be quoted `"HH:MM"`
  strings. Defaults recorded in ADR 0002.
- DATA-005: metadata database — SQLAlchemy 2 models (`xq.tracking.models`) for `data_sources`,
  `instruments`, `ingest_runs`, `raw_files`, `clean_partitions`, `cleaning_actions`, `bar_sets`,
  `bar_gaps` and `spread_stats`; instants stored as UTC int64 nanoseconds, contract terms as exact
  decimal text; Alembic migration `0001` (plain SQLAlchemy types only); SQLite foreign keys
  enforced; `xq db upgrade` and `xq db current`.
- DATA-006: UTC normalization (`xq.data.normalize`) — `ClockConvention` (`UTC`, `UTC±HH:MM`,
  `tz:<zone>`, `NY±N` for MT5 broker server time) and `normalize_local_times`, which converts
  source-clock timestamps to UTC int64 nanoseconds, flags DST-ambiguous, DST-nonexistent and
  out-of-order rows (`xq.data.flags.TickFlag`) without dropping any, and gives a stable canonical
  order. ADR 0003.
- DATA-003: source adapters — `SourceAdapter` protocol, canonical tick schema (`TICK_SCHEMA`,
  `validate_tick_frame`), file discovery, and the provisional primary adapter for MT5 tick exports
  (UTF-8/UTF-16, partial bid/ask updates carried forward in file order, `MISSING_QUOTE` flag);
  sources declared in `config/base.yaml` (`mt5_primary`, clock `NY+7`, broker to be named — ADR
  0004); deterministic synthetic MT5 fixtures around the March and November 2024 DST changes;
  gap-location tests proving the weekly open, weekly close and daily rollover gaps land at the
  expected UTC hours, and that a misdeclared `tz:Europe/Athens` clock is detected.
- DATA-004: immutable raw store (`xq.data.raw_store`) and `xq ingest --source --path` —
  content-addressed raw files (SHA-256; re-ingest is a no-op) copied read-only into
  `data/raw/<source>/<instrument>/<yyyy>/<mm>/`, a faithful Parquet mirror partitioned by UTC day
  with `raw_file_id` and `row_num`, `raw_files` / `ingest_runs` / `data_sources` / `instruments`
  manifest rows, recovery of interrupted runs, refusal to change a source's clock after ingest
  (`xq.data.provenance`), and `xq verify-raw` integrity checks. ADR 0005.
- DATA-007: non-destructive tick cleaning (`xq.data.clean`, `xq clean`) — versioned rules
  `DUP_EXACT`, `DUP_TS_DIFF_PRICE`, `NONPOSITIVE`, `CROSSED`, `SPREAD_OUTLIER`, `SPIKE`,
  `CLOSED_MARKET` and `STALE` set flag bits (bits 16–23) on per-trading-day clean partitions under
  `data/clean/<source>/<instrument>/rules=<version>/`; drops only for exact duplicates,
  non-positive and crossed quotes when configured; every rule hit logged in `cleaning_actions`
  with original values; `clean_partitions` rows with contributing raw files and sha256;
  unchanged partitions skipped, rebuilds bit-identical. Rules are causal except `SPIKE`, which is
  confirmed by later ticks. The raw mirror (schema v2) now stores each row's canonical quote;
  `xq rebuild-mirror` regenerates older mirrors. ADR 0006.
- DATA-008: bar builder (`xq.data.bars`, `xq build-bars`) — bid, ask and mid OHLC on 1m, 5m,
  15m, 30m, 1h, 4h and 1d built from clean ticks, with `available_at`, tick count, spread
  mean/median/max/close, `n_flagged`, `n_excluded`, trading day and `is_complete`; 4h/1d aligned
  to the 17:00 New York trading day; no bars for empty intervals, which become `bar_gaps` rows
  with `expected_open`; `bar_sets` rows per timeframe; versioned, bit-identical monthly files;
  causal-only tick exclusions (`SPIKE` refused). ADR 0007.
- DATA-009: hour-of-week spread statistics (`xq.data.spreads`, `xq spread-stats`) — exact
  p50/p90/p99 spreads per New York hour of week from an integer histogram on the tick grid,
  computed from usable clean ticks before the vault only, stored in `spread_stats` per window;
  test data with rollover spread widening shows the 16:xx/18:xx New York spike. ADR 0009.
- DATA-010: research catalog (`xq.data.catalog.Catalog`) — `load_ticks` and `load_bars` (one
  price basis, prices as open/high/low/close) over DuckDB with tz-aware UTC timestamps; requests
  past `vault.start` raise `VaultAccessError`, ticks at or after it and bars that become available
  after it are never returned, and `allow_vault=True` raises until DS-004 gate tokens exist.
  ADR 0010.
- DQ-001: data-quality framework (`xq.quality.registry`) — `Check` protocol returning a
  `Measurement` (larger is worse), `CheckRegistry` with `@register` and built-in discovery,
  thresholds, severity and check parameters in `config/quality.yaml` (`AppConfig.quality`),
  grading FAIL above `fail` / WARN above `warn`, validation that thresholds and registered checks
  match one to one, `run_checks` over trading-day partitions keeping the top anomalies.
- DQ-002: tick-level checks (`xq.quality.checks.ticks`) — timestamp ordering, exact and
  same-time duplicates, non-positive or crossed quotes, spread outliers against the hour-of-week
  p50, reverting spike events, stale-quote time in the active sessions, and tick-rate anomalies
  against hour-of-week norms; dropped rows are counted back in; thresholds in
  `config/quality.yaml` (plan defaults where given, proposed values marked). ADR 0011 (draft).
- DQ-003: bar-level checks on 1-minute bars (`xq.quality.checks.bars`) — OHLC consistency,
  missing minutes in the active sessions, duplicate starts, extreme adjacent-minute returns in
  robust sigma, zero-range session bars, and bid/ask/mid consistency; thresholds in
  `config/quality.yaml`. ADR 0011 (draft) extended.
- DQ-004: calendar checks (`xq.quality.checks.calendar`) — ticks while closed, ticks outside
  hours on holidays and early closes, missing minutes across all market hours, and first/last-tick
  distance from the calendar's open and close (clock errors show up an hour off; data-coverage
  edges are skipped). ADR 0011 accepted.
- DQ-006: quality runs and report (`xq.quality.validate`, `xq.quality.report`, `xq validate`) —
  per-trading-day partitions with clean ticks, 1-minute bars, calendar row, spread statistics,
  tick-rate norms (4+ weeks of history), dropped counts and data coverage; results stored in
  `quality_runs` / `quality_results` (migration 0002); `reports/quality/<run_id>/` with
  `report.md` (summary, failing days, per-check statistics, top anomalies), `summary.json`, a
  missing-minutes heatmap and a spread heatmap; pre-vault days only unless `--include-vault`.
  ADR 0012.
- DS-001: dataset specifications (`xq.datasets.spec`) — frozen pydantic `DatasetSpec` (source,
  instrument, base timeframe, price basis, UTC window, warm-up, context timeframes, feature and
  target sets by name and version, exclusions with reasons, `exclude`-only vault policy, and the
  bar build and quality run pinned on resolution); `dataset_id` = `ds-` + SHA-256 over the
  canonical spec JSON and the builder code versions, defined only for resolved specs;
  `load_spec` / `dump_spec`. ADR 0014.
- DS-002: `xq.datasets.asof.asof_join` — backward point-in-time join of a right frame onto
  decision times on availability (`right.available_at <= decision_time`, exact matches allowed,
  optional tolerance, last row wins among ties); keeps the matched row's `available_at` as a
  provenance column for the leakage audit; refuses keys that are not availability columns (no
  joins on bar start), naive timestamps and column collisions. Unit and property tests.
- DS-003: causal primitives (`xq.datasets.primitives`) — trailing `rolling` over rows or time
  spans, `expanding`, `ewm_mean` / `ewm_std`, `log_returns`, `simple_returns`, `vol_normalized`
  (scaled by the estimate known before the return), `realized_volatility`, `ewma_volatility`
  (interim sigma-hat until VOL-006), positive-only `lag`, and `resample_causal` (epoch-aligned
  bins labelled by their end); tz-aware increasing time order enforced. A hypothesis test proves
  truncation invariance for every primitive, and a lint test bans centered windows and backward
  fills in `src/`, and negative shifts outside `src/xq/targets/`.
- DS-004: vault enforcement with one-time gate tokens (`xq.datasets.vault`) — `check_window`
  lets pre-vault windows through and refuses any window past `vault.start` without a valid
  `GateToken` (`<token_id>.<secret>`); tokens are verified against `vault_tokens` (secret stored
  as SHA-256), refused when unknown, revoked, expired or redeemed by another run, and every
  granted read logs a `vault_access_granted` warning and a `vault_access_log` row (migration
  0003). The catalog's `load_ticks` / `load_bars` take `vault_token=` (replacing the always-failing
  `allow_vault=`) and an optional `engine` and `run_id`. Nothing in the library issues tokens yet
  (GATE-002). ADR 0015.
- DS-006: leakage harness (`xq.datasets.leakage`) — `check_feature_causality` runs truncation
  invariance and future perturbation at 25 seeded decision times plus an availability audit of
  provenance columns; `correlation_scan` fails features with |corr| > 0.9 to a target at lag 0
  unless the pair is explicitly allowed after review; `check_target_bounds` proves a target uses
  no quotes after `label_end`, none before the decision time and no sigma-hat other than at t.
  `tests/leakage/` catches all five planted leaks from the plan (centered rolling mean,
  full-sample z-score, `bfill`, higher-timeframe join on bar start, target shifted into features)
  and three planted target leaks, passes their correct counterparts, and runs every DS-003
  primitive and the availability join through the harness (`assert_causal` fixture). ADR 0016.
- DS-005: dataset builder (`xq.datasets.builder`, `xq dataset build <spec>`, `xq dataset show`)
  — resolves a spec (configured bar build, latest overlapping non-vault quality run, digest of the
  calendar and instrument config), reads bars only through the vault-enforcing catalog, drops
  incomplete bars and excluded trading days, computes the spec's feature set, and writes
  `data/datasets/<id>/{features.parquet, spec.yaml, manifest.json}` atomically; the manifest has
  row count, decision-time range, column types, per-file and combined SHA-256, git sha, code
  versions, bar set ids, quality run ids and exclusions; `dataset_versions` table (migration
  0004). Rebuilding an existing id verifies it: identical content is a no-op, different content
  or a tampered file raises `DatasetIntegrityError`; `load_dataset` re-hashes before reading.
  Built-in feature set `base.v1` (decision-bar values plus context bars joined on availability)
  passes the leakage harness. `experiments/configs/ds_base.yaml` is the base research spec.
  ADR 0017.
- DS-007: calendar and session columns known in advance (`xq.datasets.calendar_columns`, part of
  `base.v1`) — trading day and weekday of the decision time, open / early-close / US and UK
  holiday flags, minutes to market close, `in_<session>` and minutes since open for Tokyo, London,
  New York and the London–New York overlap, minutes to and since every event anchor (LBMA AM/PM,
  COMEX open, US 08:30 release, rollover; seven-day lookup cap), and `in_<anchor>_window` flags
  from `event_windows` in `config/sessions.yaml` (US release −5/+30 min, rollover ±15 min,
  proposed). Tested against the session table across US and UK DST and a US holiday, and through
  the leakage harness. ADR 0018.
- DQ-007: quality gate in the dataset builder (`xq.quality.gate.gate_partitions`) — every
  trading day a dataset reads (warm-up, window, context timeframes, explicit exclusions) is checked
  against the pinned quality run; FAIL or unvalidated days refuse the build with a message naming
  each day and failing check unless the spec excludes them with a reason; excluded days are
  dropped from every timeframe; the manifest records `included_partitions`, `warn_partitions`
  (with warning checks) and `excluded_partitions` (with reason and failing checks). A test injects
  an OHLC error into 1-minute bars and shows the gate blocking that day. ADR 0019.
- EXP-001: experiment registry schema and API (`xq.tracking.registry`) — tables `hypotheses`
  (versioned by text hash, with the exact YAML of every version), `experiments` (bound to a
  hypothesis version), `runs` (git sha, config hash and JSON, dataset id, lock hash, seed, host,
  timings, status), `trials`, `metrics` (optionally per fold) and `artifacts` (with SHA-256),
  migration 0005; append-only API returning frozen records — create, read and one-way lifecycle
  updates (hypothesis superseded, run finished or failed), no deletes. ADR 0020.
- EXP-002: hypothesis pre-registration (`xq.tracking.hypotheses`, `xq exp register <path>`,
  `xq exp hypotheses`) — `HypothesisDoc` with the plan's fields plus title and trial family;
  registration requires non-empty success and falsification criteria and planned tests, a positive
  trial budget, a discovery window that ends before the evaluation window, an evaluation window
  that ends by `vault.start`, and a file named after its id; the exact file text is locked by
  SHA-256 and any edit becomes a new version (the old one superseded but readable). Template in
  `experiments/hypotheses/TEMPLATE.yaml`. ADR 0021.
- EXP-003: run context (`xq.tracking.runs.experiment_run`) — records git sha, a hash of the
  run's configuration with the application config hash, the dataset id (verified against its
  manifest), the `uv.lock` SHA-256, the seed (global seeding plus a seeded generator), host and
  timings on the hypothesis's open experiment; logs metrics and artifacts; marks the run failed if
  the block raises. Confirmatory runs (the default) are refused on a dirty or unidentifiable git
  tree or without `uv.lock`; `exploratory=True` records a non-confirmatory run (`+dirty` sha).
  ADR 0022.
- EXP-004: trial counter (`xq.tracking.trials`, `RunContext.record_trial`, `xq exp trials`) —
  every evaluated configuration of a live run is recorded (family, config hash, test-fold flag,
  Sharpe, return series under `data/artifacts/`); `trial_count` gives per-family and global trial
  and test-evaluation counts, the Sharpe variance, and the effective number of independent trials
  (average-linkage clustering of return correlations, cut at rho 0.7, at least 20 common
  observations; trials without returns count as independent), with parameters in
  `experiments.trial_clustering` (proposed). New dependency `scipy` (hierarchical clustering). ADR
  0023.
- TGT-001: target framework (`xq.targets.base`, `xq.targets.kinds`) — target sets defined in
  `config/targets.yaml` (`kind`, `horizons`, `price_refs`, `params`), expanded into `TargetSpec`s
  and computed by a `TargetKind` (`expand`, causal `sigma`, `compute`, `lookahead`, code
  version); every target carries `value`, `label_start`, `label_end` and `scale`; datasets with a
  target set write `targets.parquet` in long form, computed month by month from clean ticks
  (unusable and excluded-day ticks removed, never past the vault, quote days gated by DQ-007);
  definitions are hash-locked in `target_sets` (migration 0006) and part of the dataset id; the
  schema guard `check_feature_matrix` refuses target columns and reserved prefixes (`tgt_`,
  `fwd_`) in feature matrices. ADR 0024.
- TGT-002: execution-aware forward returns (`xq.targets.returns`, target set `fwd_returns.v1` in
  `config/targets.yaml`) — entry at the first usable tick at or after t + 1 s latency and exit at
  the first at or after t + h + latency; long buys the ask and sells the bid, short sells the bid
  and buys the ask, mid is the research variant; no label when a fill would be more than 300 s
  late (daily break, weekend, excluded day, end of data); `_vol` variants divide by the interim
  EWMA sigma-hat (span 96 bars) scaled to the horizon; horizons 15m, 1h, 4h, 1d (24 targets).
  Every target passes the leakage bound checks; `ds_base.yaml` includes the set. ADR 0025.
- WF-001: splitters (`xq.validation.splitters`) — `WalkForwardSplitter` (expanding or rolling,
  calendar-span windows, validation before test, `step >= test_len`), `PurgedKFold` and
  `CombinatorialPurgedCV`; training and validation labels purged by `label_end` with the embargo as
  a gap before the next window; `Fold.train_end` is the information cutoff; unlabelled samples are
  predicted but never trained on. ADR 0027.
- WF-006: splitter guard property tests (`tests/property/test_splitters.py`) — for random sample
  spacing, label horizons (some missing) and splitter settings, every label used for fitting or
  selection ends before `test_start - embargo`, test windows never overlap, purging removes exactly
  the labels that would reach the next window (no over-purging), and purged k-fold / CPCV never
  train on a test group's span plus embargo.
- WF-002: walk-forward runner (`xq.validation.walkforward`) and model interface (`xq.models.base`:
  `ModelSpec`, `ModelConfig`, `Estimator`) — per fold, grid candidates are fitted on training rows
  and selected on validation rows, then the selection predicts the test rows; per-fold seeds make
  serial and `spawn`-parallel runs identical; fold outputs are cached under a key that includes a
  digest of the rows read; `run_walk_forward` applies the target schema guard, records
  `fold_results` (migration 0007), logs stitched metrics and records the evaluation as a trial.
  Tests include an AR(1) hit rate matching 1/2 + arcsin(φ)/π and the purging demonstration.
  ADR 0028.
- WF-003: out-of-sample prediction store (`xq.validation.predictions`) — Parquet under
  `data/predictions/<experiment_id>/<run_id>/` with `decision_time, fold_id, model_version,
  feature_set_version, y_true, y_pred, p_raw, p_cal, train_end`. The writer refuses the whole frame
  if any decision time is not after `train_end + embargo` (or is naive, duplicated or unsorted),
  and records every file as a run artifact. `run_walk_forward` stores its predictions through it.
- BT-001: cost model (`xq.backtest.costs.CostModel`, `config/costs/placeholder.yaml`,
  `backtest:` in `config/base.yaml`) — commission per lot and per notional, slippage
  `(fixed_bps + k·σ̂_1m) × window multiplier` from the calendar columns, financing at each open
  trading day's 17:00 New York rollover by side with a triple weekday, hour-of-week spread
  fallback, latency and maximum fill delay. The placeholder values are PROVISIONAL (no broker
  named). Golden tests include the triple rollover and a Good Friday with no rollover. ADR 0029.
- BT-002: vectorized screener (`xq.backtest.vectorized.run_vectorized`) — target exposures filled
  at the first quote `latency` of market time after the decision, buying at the ask and selling at
  the bid plus slippage (never at the signal bar's close or at mid); late fills are missed and
  retried; daily P&L by trading day with `gross − spread − slippage − commission − financing =
  net` exactly, financing at each rollover; holding-episode trades with net P&L. Golden tests
  include a hand-computed round trip, four nights over the triple rollover, a Friday-close
  decision filled at the Sunday reopen and a side flip. ADR 0030.
- BT-003: performance metrics (`xq.backtest.metrics`) — annual return, CAGR, volatility, Sharpe and
  Sortino on daily returns (252 periods, zero risk-free rate), maximum drawdown (fraction and USD)
  and its duration, Calmar, recovery factor, CVaR 95/99, worst day, time in market, average
  exposure, turnover, trade count, win rate, average win and loss, expectancy and profit factor;
  hand-computed tests and independent pandas cross-checks. ADR 0030.
- BASE-006: forecast evaluation (`xq.validation.forecast_eval`) — log loss, Brier, expected
  calibration error and reliability curves (equal-width bins), AUC (Mann–Whitney, ties halved),
  MSE, MAE, QLIKE, and `loss_series` per-observation losses for Diebold–Mariano tests; matches the
  scikit-learn documentation examples and scipy's Mann–Whitney statistic. Walk-forward fold
  metrics and validation selection now use it.
- VAL-001: Sharpe inference (`xq.validation.sharpe`) — per-period Sharpe ratio with i.i.d.
  (Lo), non-normal (Mertens) and GMM/Newey–West standard errors, Lo's eta(q) annualization
  factor, stationary-bootstrap percentile interval and minimum track record length. Verified by
  closed forms and Monte Carlo on normal, skewed and AR(1) returns (no published table was
  reproduced for this task). ADR 0031.
- VAL-002: probabilistic and deflated Sharpe ratios (`xq.validation.dsr`) — PSR, expected maximum
  Sharpe ratio of N trials, DSR, and `deflated_sharpe_for_family` using the registry's effective
  trial count and trial Sharpe variance (trials record annualized Sharpe ratios). Reproduces the
  published example (DSR 0.9004); on pure-noise families the selected best passes DSR > 0.95 in at
  most 8 % of simulations. ADR 0031.
- VAL-005: forecast comparison (`xq.validation.forecast_eval`) — Diebold–Mariano with the
  Harvey–Leybourne–Newbold correction, Giacomini–White conditional predictive ability test and
  the Model Confidence Set (T_max, stationary bootstrap, MCS p-values). Size checked on simulated
  nulls and power on simulated alternatives. ADR 0031.
- VAL-007: evidence policy (`config/gates.yaml`, approved by the owner) loaded as
  `AppConfig.gates` — conventions (daily net returns, annualization by
  `backtest.periods_per_year`, effective trial count with a raw/effective review flag, one-sided
  tests, stationary bootstrap with 10,000 resamples and a Politis–White block length of at least 5
  days) and gates R1–R4, validated strictly; only `gates.yaml` may set them (base, profile,
  `XQ_GATES__*` and `--set` are refused); `GatesConfig.criteria()` fixes every threshold's boundary
  rule; `gates_hash`. VAL-001 gains `politis_white_block_length` (matches `arch` 8.0.0 to 1e-9,
  recovers the AR(1) optimum), `gate_block_length`, `bootstrap_distribution` and
  `bootstrap_sharpe` (percentile interval and null-centred one-sided p-value, nominal size on
  zero-mean AR(1) returns). ADR 0032.
- BASE-001: forecast baselines (`xq.models.baselines.FORECAST_BASELINES`) for the walk-forward
  runner, fixed and untuned — `zero_return`, `random_walk` (persistence: the log return of the
  latest completed bar of the horizon's timeframe, joined on availability; `random_walk_columns`),
  `historical_mean` (the training fold's mean, expanding with the windows) and `climatology` (the
  training fold's frequency of positive targets). Known outputs per fold are tested.
- BASE-002: rule baselines with fixed parameters (`xq.models.baselines`) — `buy_and_hold`,
  `time_series_momentum`, `zscore_reversion`, `ma_crossover`, `donchian_breakout` (channel exit
  and ATR stop fixed at entry), each optionally volatility-targeted (`VolTargetConfig`), computed
  on signal bars (the dataset's distinct context bars indexed by availability, `signal_bars`) and
  placed at decision times by an as-of join on availability (`positions_at`); the random-entry null
  (`random_entry`, `random_entry_null`) keeps a template's holding episodes — trade count, holding
  times, exposure paths and sides — and randomizes their timing uniformly. Hand-built series give
  known positions and screened trades; every rule passes the leakage harness, which catches a
  planted bar-start placement. ADR 0033.
- BASE-005: baseline board (`xq.models.board.run_baseline_board`, `xq baselines run --dataset <id>
  [--target ...] [--config ...] [--exploratory]`, `experiments/configs/baselines/board.yaml`) —
  forecast baselines through walk-forward per target (losses with bootstrap intervals and a
  one-sided Diebold–Mariano test against `zero_return`), forecast-sign and rule strategies (plain
  and volatility-targeted) screened with the cost model on identical folds; daily net returns on
  every out-of-sample trading day with the gate conventions: Sharpe with bootstrap interval,
  one-sided p-value, three standard errors, PSR, DSR with the family's effective trial count,
  MinTRL and the random-entry null p-value, plus bootstrap intervals for annual return,
  volatility, Sortino and maximum drawdown; every strategy recorded as a trial; report
  `reports/baselines/<dataset>/<run>/` (`board.md`, `board.json`, `returns.parquet`) with every
  net figure marked "screening, placeholder costs". BT-002 gains `required_quotes` (the quotes a
  screen can read; identical results from the subset) and DS-005 `usable_quotes` (shared quote
  filter). Draft pre-registration `experiments/hypotheses/H-0001.yaml` (not registered). ADR 0034.
- EDA-001: research report framework and discovery window (`xq.research.reports`,
  `xq.research.eda.data`, `config/eda.yaml` loaded as `AppConfig.eda`, `xq research eda --dataset
  <id> --hypothesis <H> [--end ...] [--exploratory]`) — `ReportBuilder` writes Markdown sections,
  full-precision CSV tables, PNG figures without software or time metadata, `metadata.json`
  (dataset, discovery window, git sha, `uv.lock` hash, seed, configuration hashes) and a SHA-256
  manifest, byte-identical for the same dataset, configuration, commit, lockfile and seed; every
  file is a run artifact under `reports/eda/<dataset>/<run>/`. The discovery window is the first
  `discovery.fraction` (0.5, provisional) of the span from the dataset's start to `vault.start`,
  ending at a trading-day start, or everything before a fixed `discovery.end`; bars available
  after it are refused (never cut silently), also for an explicit `--end`; returns are kept only
  between bars adjacent in market time. EDA runs record no trials. Synthetic data only (ADR 0035).
  ADR 0036.
- EDA-006: cost-to-volatility table and horizon admission (`xq.research.eda.horizons`,
  `xq research admit-horizons --report <dir>`) — round-trip cost of every holding period from the
  BT-001 cost model (spread, commission, slippage with a trailing one-minute sigma-hat, financing
  at the mean of the long and short rates) over the mean absolute log return, per horizon (1m to
  1d) and session; horizons with an overall ratio above 0.3 are excluded; every figure is labelled
  "screening, placeholder costs"; the report's `admission.yaml` reaches `config/horizons.yaml`
  only from a confirmatory, unaltered report. No values are written to `config/horizons.yaml`
  (ADR 0035). ADR 0037.
- EDA-002: return distributions (`xq.research.eda.distributions`, `xq.research.eda.bootstrap`) —
  per timeframe mean, standard deviation, skewness and excess kurtosis with stationary-bootstrap
  intervals (one resample at a time; mean block the Politis–White length of squared returns, at
  least five trading days of bars, at most a tenth of the series), Jarque–Bera, Student-t fit with
  QQ plots against the normal and the t, Hill tail indices and moments by year. Tested against
  scipy, a Pareto tail, a t sample and an AR(1). ADR 0038.
- EDA-003: dependence (`xq.research.eda.dependence`) — ACF by FFT and PACF by Durbin–Levinson of
  returns, absolute and squared returns up to one trading day of lags, with i.i.d. and
  heteroskedasticity-robust bands; lags are flagged against the robust band. Matches statsmodels;
  the robust band is wider than the i.i.d. one on a GARCH(1,1) simulation and equal on i.i.d.
  data. Adds `statsmodels` as a development dependency (the reference in tests only). ADR 0038.
- EDA-004: seasonality (`xq.research.eda.seasonality`) — hour of week (New York), day of week,
  month, sessions and event windows (the dataset's US-release and rollover windows plus the LBMA
  auctions) for return, absolute return, tick count and spread: effect sizes, cluster-robust
  standard errors (trading week; month), Bonferroni-corrected Student-t intervals and split-half
  stability labels. Injected hour-of-week effects are found and stable; an effect that stops
  half-way is unstable; noise stays within the family-wise error rate. ADR 0038.
- EDA-005: trend and reversion descriptives (`xq.research.eda.trend`) — Lo–MacKinlay variance
  ratios with the heteroskedasticity-robust z*, sign runs with run-length counts against
  independent signs, and buy-and-hold drawdown episodes, time under water and the longest
  underwater spell. Variance ratios match AR(1) theory. ADR 0038.
- EXP-005: experiment conclusions and the research log (`xq.tracking.conclusions`, migration
  `0008` with table `conclusions`, `xq exp close <experiment> --conclusion <yaml>`, `xq exp
  audit`, `experiments/conclusions/TEMPLATE.yaml`, `docs/research/log.md`, `paths.research_log`) —
  an experiment closes only with a verdict (supported, rejected, inconclusive) and non-empty
  Observed / Evidence / Interpretation / Limitations / Action; closing is refused while a run is
  running, and "supported" needs a finished confirmatory run; the entry is appended to the
  research log before the database commit; `xq exp audit` lists experiments still without a
  conclusion and exits 1 if any. ADR 0039.
- STAT-001: stationarity battery (`xq.research.stats.stationarity`, `config/stats.yaml`) — ADF
  with AIC lags, Phillips-Perron, KPSS around a level and a trend, and Zivot-Andrews with one
  level break (its date reported), each as a typed `StatResult` (statistic, p-value, lags,
  null and alternative, assumptions, decision, recorded warnings), with a joint verdict
  (stationary, unit root, conflicting, inconclusive, mixed). Recovery: a random walk is not
  rejected by ADF (size near 5 % over 200 draws) and reads as a unit root; a stationary AR(1)
  reads as stationary; a level shift is found near its date. Adds `arch` and `statsmodels` as
  runtime dependencies (unit-root tests, GARCH, ARMA; statsmodels was a development dependency)
  and the recovery registry `xq.research.recovery`. Simulated data only. ADR 0043.
- STAT-002: dependence tests (`xq.research.stats.dependence`) — Ljung-Box on returns, absolute
  and squared returns, the heteroskedasticity-robust portmanteau Q* on returns (robust
  autocorrelation standard errors of EDA-003) and ARCH-LM, at the lags of `config/stats.yaml`,
  with Holm-adjusted p-values across the lags of each family. Matches statsmodels. Recovery: on a
  GARCH(1,1) simulation Ljung-Box on squared returns and ARCH-LM reject; Q* keeps its size on GARCH
  returns (at most 10 % rejections at 5 % over 150 draws) where the plain Ljung-Box over-rejects;
  an AR(1) is detected. ADR 0043.
- STAT-003: variance-ratio tests (`xq.research.stats.variance_ratio`) — Lo-MacKinlay robust z* at
  2 to 64 bars with the Chow-Denning joint test; by slice (session, volatility regime) with q-bar
  windows inside contiguous runs only and Holm across slices; volatility-regime labels from the
  trailing volatility before each return with cut-offs from reference rows. Recovery: on
  Ornstein-Uhlenbeck increments VR(q) is below 1 and matches (1 - phi^q) / (q (1 - phi)), and the
  joint test rejects; on a random walk it does not; its size on GARCH returns stays at most 10 %;
  slices find reversion only where it is. ADR 0043.
- STAT-006: ARMA walk-forward forecasts (`xq.research.stats.arima`) — ARMA(p, q) of 1-bar log
  returns by exact maximum likelihood per training fold (AR(p) by AIC for `ar_aic`), causal
  h-bar forecasts, the `ArmaForecast` estimator for the walk-forward runner, and `arma_study`:
  every model and benchmark (`zero_return`, `random_walk`) on identical folds, Diebold-Mariano
  on squared errors (two- and one-sided, `diebold_mariano_less` in `xq.validation.forecast_eval`)
  with Holm across horizons, "useful evidence" only out of sample, one trial per (model, horizon)
  inside a run. Recovery: AR(1) with phi = 0.5 and ARMA(1, 1) recovered, AIC finds an AR(2),
  forecasts match the closed form and statsmodels and are causal, an AR(1) beats both benchmarks
  after Holm and i.i.d. returns do not. ADR 0043.
- STAT-008: statistical verdict report framework (`xq.research.stats.verdict`) — builders turn
  STAT-001, STAT-002, STAT-003 and STAT-006 results into verdicts in the Observed / Evidence /
  Interpretation / Limitations / Action format (every field required, each citing the recovery
  tests its method passed) with the plan's reading rules (non-rejection is not a unit root, ARCH
  effects are not return predictability, a variance ratio counts only through the joint test,
  only out-of-sample DM evidence after Holm is "useful evidence"), and `build_verdict_report`
  writes them as a deterministic report. Exercised on simulated results only; no report on real
  data exists. ADR 0043.
- VOL-001: range estimators and ATR (`xq.research.volatility.estimators`, `config/volatility.yaml`)
  — trailing close-to-close, Parkinson, Garman-Klass, Rogers-Satchell and Yang-Zhang per-bar
  variances on any price basis, and Wilder's ATR in price units and relative to the close. Match
  hand computations; on a simulated Brownian path with opening gaps the range estimators recover
  the intraday variance and Yang-Zhang and close-to-close the total; trailing (later bars never
  change earlier values). ADR 0044.
- VOL-002: realized measures and the diurnal factor (`xq.research.volatility.realized`) — RV,
  bipower variation, the jump component and the intraday return per UTC hour and per trading day
  from 1m or 5m returns inside one trading day (the daily-break return is left out), indexed by
  the period's decision time; `DiurnalFactor` of the intraday variance pattern (buckets since the
  17:00 New York roll, day-standardized), fitted only on rows available by `train_end`. Match hand
  computations; hourly RVs add up to the daily RV; bipower separates simulated jumps up to its
  known finite-sample contamination; an injected pattern is recovered; perturbing test data leaves
  the factor unchanged while a full-sample fit moves. ADR 0044.
- VOL-003: volatility benchmarks (`xq.research.volatility.benchmarks`) and the `VolForecaster`
  interface (`xq.models.volatility`: `fit`, `predict_variance`, `predict` on a periods frame
  indexed by decision time) — `rolling_22`, `ewma_0.94`, `ewma_0.97` (RiskMetrics) and `har` (one
  OLS per horizon on training rows whose future is training, floored), deseasonalized on hourly
  periods with the train-only diurnal factor. EWMA and rolling RV match hand computations; HAR
  recovers the coefficients of a simulated HAR process; every benchmark is causal; the diurnal
  adjustment is fitted on training periods only. ADR 0044.
- VOL-004: GARCH family (`xq.research.volatility.garch`) — GARCH(1,1), GJR-GARCH and EGARCH
  with normal, Student-t and skewed-t errors as `VolForecaster`s, refitted per fold on scaled
  training returns, with analytic multi-step forecasts (EGARCH beyond one step simulated with a
  seeded distribution) and fit diagnostics. Recovery: GARCH(1,1), GJR leverage, EGARCH and
  Student-t degrees of freedom recovered within tolerance on simulated data; forecasts match the
  GARCH recursion and its multi-step closed form; causal and reproducible. ADR 0044.
- VOL-005: volatility evaluation (`xq.research.volatility.evaluate`) — every forecaster on the
  same walk-forward folds and target (RV summed over the horizon), fitted per fold on training
  periods only: QLIKE (primary) and MSE, Mincer-Zarnowitz with Newey-West errors, Diebold-Mariano
  against HAR and the EWMA default, the 90 % Model Confidence Set, fold-level scores, breakdowns
  by session and by volatility regime (cut-offs from training rows), one trial per model inside a
  run. Recovery: HAR and EWMA scored with QLIKE on identical folds and refit by hand; the true
  GARCH variance ranks first, stays in the MCS and passes Mincer-Zarnowitz while a doubled
  forecast fails. ADR 0044.
- VOL-006: sigma-hat selection and serving (`xq.models.volatility`) — `select_forecaster` keeps
  `ewma_0.94` unless a model is in the 90 % MCS and beats it by one-sided Diebold-Mariano on QLIKE
  (p < 0.05), then picks the lowest QLIKE among such models, recording every candidate's reason;
  `serve_sigma` serves the selected forecaster's sigma-hat per fold, fitted on training periods
  only; `board_forecasters` is the volatility board (four benchmarks, nine GARCH-family models).
  Tested: the rule defaults to EWMA when nothing beats it, promotes only an eligible model, and
  keeps EWMA end to end when EWMA is the true model; served sigma-hat matches a per-fold refit and
  ignores later data. Nothing is promoted: sigma-hat stays the interim EWMA of `fwd_returns.v1`.
  ADR 0044.
- BASE-003: statistical baselines on the boards — the `ar1` forecast baseline (an AR(1) of the
  decision bars' returns per training fold, iterated to the target's horizon, order fixed) runs on
  the baseline board like the other forecast baselines (metrics, DM against `zero_return`, a
  forecast-sign strategy), shown end to end on a synthetic dataset; EWMA and HAR are the
  volatility board's benchmark entries (the selection's default and DM reference). The ARMA model
  moves to `xq.models.arma` (re-exported by `xq.research.stats.arima`). H-0001's `board.yaml` is
  unchanged: adding `ar1` would raise its approved trial budget from 36 to 40 (owner decision).
  ADR 0045.
- BT-004: event core of the event-driven backtester (`xq.backtest.events`, `xq.backtest.engine`) —
  `TickEvent`, `BarEvent`, `SignalEvent`, `OrderEvent`, `FillEvent`, `TimerEvent` and, for
  execution without ticks, `ExecutionBarEvent`; an `EventQueue` ordered by (timestamp, rank,
  sequence), where the rank puts timers before order arrivals before quotes before fills before
  signal bars before intents at one instant; a `SimulationClock` that never goes back;
  `MarketData` in tick mode (quotes, with signal bars built by DATA-008 `build_bars`) or bar
  mode (one-minute execution bars, open then range); the `Strategy.on_bar(bar, ctx)` interface;
  and the `EventEngine` loop, which stamps each intent's id and decision time, refuses decisions
  taken while the market is closed (ADR 0032), and schedules rollover financing, day ends, order
  expiries, time stops and weekend exits as timers. The decision-chain schemas `TradeIntent`,
  `RiskDecision` and `OrderIntent` (`xq.signals.schema`; an order can only be built from an
  approved decision) and the PLACEHOLDER pass-through risk approver (`xq.risk.placeholder`, no
  risk checks until RISK-005) come with it. Tested: queue order at equal instants, the clock,
  the data stream, schema validation, the placeholder's sizing, and a deterministic engine trace
  with stub components. ADR 0049.
- BT-005: broker simulator (`xq.backtest.broker_sim.SimulatedBroker`) — market, limit and stop
  orders with latency in market time; fills at the first quote at or after arrival on the correct
  side plus slippage (never at mid), expiry after the maximum fill delay; stops fill at the first
  quote beyond the stop (gap fills at the gapped price), limits at their price without
  improvement; SL/TP as an OCO bracket on the whole position; no fills while the market is
  closed; in bar mode (one-minute bars) market orders fill at the next open, and a bar touching
  both legs of a bracket resolves to the stop loss and is counted as ambiguous; orders rejected
  for insufficient margin or when the position changed since their decision; newer orders
  replace working orders of earlier intents. Every fill carries its spread, slippage and
  commission decomposition. Golden hand-computed tests cover each rule. ADR 0049.
- BT-006: portfolio accounting of the event tier (`xq.backtest.portfolio.Portfolio`) — a USD CFD
  account for one instrument: cash moved by realized P&L, commissions and financing; FIFO lots
  with per-trade price P&L, commission and financing shares; unrealized P&L at the latest mid;
  margin used; financing at each rollover on the position held over it (both sides a cost while
  costs are provisional, triple on Wednesday). Equity = cash + unrealized is checked against an
  independent mark-to-market equity after every event of an engine run and in a hypothesis
  property test; `daily_frame` gives the screener's daily layout for the BT-003 metrics. Tested:
  a hand-computed FIFO case, a flip, financing over the triple rollover on longs and shorts.
  ADR 0049.
- BT-007: decision ledger (`xq.backtest.ledger.Ledger`) — every intent, refusal, risk decision
  (with rejections and reasons), order, bracket leg, fill, rejection, cancel and expiry in order,
  linked by intent, decision, order and fill ids; `check_links` lists every broken link (above
  all an order without an approved decision); Parquet ledger plus a summary by kind and reason.
  `run_event_backtest` wires strategy, placeholder risk approver, broker, portfolio and ledger
  and returns an `EventBacktestResult` (the screener's result layout plus the ledger, equity per
  signal bar, brackets and the ambiguous-bar share). Tested: a golden hand-computed run
  (`tests/fixtures/golden_trades/`), every order linked to an approved decision across random
  runs, planted broken links detected, the placeholder named on every decision, Parquet round
  trip, determinism. The broker now records fills as they happen, and a limit fill's reference
  quote is its limit on its side (half-spread, no slippage). ADR 0049.
- BT-008: session constraints (`xq.backtest.constraints.SessionConstraints`, `backtest.event` in
  `config/base.yaml`) — entry blackouts in the 16:45-18:15 New York rollover window (C-3), the US
  data release window and the last 60 minutes before a weekly close (weekends and full-day
  holidays); intents opening or flipping a position there are refused before the risk decision,
  market entries arriving or meeting their first quote there are rejected or cancelled, resting
  entries wait; exits are never blocked. Optional flat-before-weekend exit 30 minutes before the
  weekly close. Windows are exact UTC intervals, shown to agree with the dataset calendar columns
  across DST changes. `backtest.event` also holds the PROVISIONAL margin rate (0.05) and the
  reconciliation tolerance (0.05 of total costs). ADR 0049.
- BT-009: reconciliation of the two tiers (`xq.backtest.reconcile.reconcile`) with shared
  market-order strategies (`xq.backtest.strategies.ExposureStrategy`, `RuleStrategy`, which runs
  a BASE-002 rule bar by bar) — the daily equity difference against 5 % of total costs
  (`backtest.event.reconcile_tolerance`), and every difference explained: the event tier's
  executed positions replayed through the screener split it into an itemized execution effect
  (sizing, event-only rules, follow-ons) and a mechanical residual that must stay below one
  cent. Tested on three rule baselines at 100,000 and 10,000,000 USD with sigma-scaled slippage
  and window multipliers, missed and closed decisions, blackouts as explained differences, and
  planted disagreements (commission, latency) caught as unexplained. `CostModel.slippage_bps_at`
  prices one fill from a per-minute multiplier table built once per trading day. ADR 0049.
- BT-010: backtest report for both tiers (`xq.backtest.report.build_backtest_report`) — the cost
  basis on every net figure and the risk approver's label (PLACEHOLDER in Sprint 11), the BT-003
  metrics, the cost decomposition (gross, spread, slippage, commission, financing, net) with the
  cost-fragility flag (gross below 1.5x costs), equity and drawdown, compounded monthly returns,
  the trade distribution, exposure by session, the ambiguous-bar share with its resolution, and
  the ledger summary of the event tier. `write_backtest` stores the report and the ledger as run
  artifacts and records the backtest in the new `backtests` table (migration 0009). Tested on a
  baseline rule through both tiers, bar-mode ambiguity, determinism and an experiment run.
  ADR 0049.
- RISK-001: risk state (`xq.risk.state`) — `RiskState` (equity, peak, drawdown and its sticky
  worst, the trading day's starting equity and P&L, position, notional, margin, consecutive losing
  round trips, entries today) updated by `RiskStateTracker` from equity observations at every
  decision and trading day's end and from fills; the event engine records each observation as an
  `account` ledger row, and `rebuild_risk_state` replays the ledger into the same state, equal to
  the live state at every decision (tested). `MarketState` carries the latest quote, its age, the
  daily sigma-hat, a reference spread, the sessions and the kill switch for the risk engine.
  ADR 0052.
- RISK-002: position sizing (`xq.risk.sizing`) and risk profiles (`config/risk/default.yaml`,
  `RiskConfig`, `backtest.risk_profile`) — fixed-fractional (0.5 % of equity to the stop, the
  owner's default) or volatility-targeted size, capped by the strategy's requested exposure,
  scaled by the calibrated win probability and the drawdown throttle (5 % → 15 %), rounded down
  to the lot step. Hand-computed cases and hypothesis properties: never above the request or the
  risk budget, lot-step multiples within the lot range, monotone scales. ADR 0052.
- RISK-003: limits and halts (`xq.risk.limits`) — halts on new exposure (sticky drawdown halt,
  daily loss halt, cooldown after consecutive losing round trips, entries per day), each
  triggering exactly at its threshold (tested at and a hair below every one), and caps on every
  target (lots, notional with a correlated-exposure hook, margin use, per-session exposure),
  rounded down to the lot step; a hypothesis property shows a capped target never exceeds any
  limit or the requested size and keeps its side. ADR 0052.
- RISK-004: stop policy (`xq.risk.stops.check_stops`) — every long or short intent needs a stop
  on the losing side of its entry reference (the side's quote, or the order's price); a stop
  closer than 3 spreads (at least a tick) is widened outward to the tick and becomes the adjusted
  stop; one farther than 5 daily sigmas, or any stop without a sigma-hat, is refused; targets must
  be on the winning side and time stops after the decision (allowed in addition to the price
  stop). Tested on hand-computed bounds, inclusive at the sigma bound. ADR 0052.
- RISK-005: the risk engine (`xq.risk.engine.RiskEngine`) and the `OrderIntent` construction
  rule — `evaluate(intent, state, market)` is pure and deterministic: exits always approved; new
  exposure passes the data checks (a quote, a calibrated win probability), the stop policy,
  sizing, the halts and the caps; a refused intent still closes a position on the other side
  (reason `risk rule: …`); every decision carries the full limits snapshot and
  `config_version` = profile version @ profile hash. Decisions carry a private *issued* mark set
  only by the engine; `OrderIntent` refuses a decision that is not approved or not issued (a
  hand-built, revalidated or copied one), and `model_construct` / `model_copy(update=)` are
  disabled; an AST architectural test fails on any `OrderIntent`, `RiskDecision` or
  issue call under `src/` outside the risk engine (planted violations caught). The event
  engine takes only a `RiskEngine`, assembles the market state (quote, daily sigma-hat from a
  supplied series or the interim EWMA of signal bars, capped sessions) and hands the same
  sigma-hat to strategies; `ExposureStrategy` and `RuleStrategy` attach stops in sigma-hat
  units; `TradeIntent` gains `p_win` and `calibrated`; `RiskDecision` gains `order_type` and
  `price`. Reconciliation labels exits a risk rule forced as event rules. ADR 0052.
- RISK-006: kill switch and data-health breakers (`xq.risk.kill_switch`) — a kill switch on
  while a file exists, an environment variable (`XQ_KILL_SWITCH`) is true or it is engaged by
  hand, and breakers for a stale quote (older than 120 s) and an abnormal spread (above 5 x the
  median of the last 500 quotes); all block new exposure in `RiskEngine.evaluate`, never an exit.
  The event engine reads the switch at every decision, keeps the spread reference, and with the
  `flatten` policy sends a `flat` intent while the switch is on; backtests honour a kill switch
  only when given one. Tested at the thresholds and end to end (stale and wide quotes refused, a
  switch turned on mid-run, with and without flattening). ADR 0052.
- SIGNAL-001: signal schemas (`xq.signals.schema`) — `Forecast`, `RegimeState`,
  `SignalCandidate` and `SignalRecord` in the plan's shapes (plus ids, `p_se`, the barrier target
  and side, the calibration id), frozen and validated; JSON Schemas of all seven decision-chain
  interfaces exported to `docs/specs/interfaces/` (`write_json_schemas`), kept in sync by a test;
  JSON round trips, and an audit-completeness test of the record against the plan's list.
  ADR 0052.
- SIGNAL-002: expected value in sigma units (`xq.signals.ev`) — EV_gross = p·TP − (1 − p)·SL,
  EV_net = EV_gross − round-trip cost, a candidate qualifying only if EV_net > θ and p > p_min
  (strict); the conservative variant uses the lower bound p − z·p_se; costs in basis points
  convert to sigma units. Golden cases for both variants and the strict thresholds. ADR 0052.
- SIGNAL-003: signal filters (`xq.signals.filters`) — the regime filter as an interface
  (`RegimeFilter`, taking the filtered `RegimeState`) with only a clearly marked PLACEHOLDER
  pass-through until REG-007; session and blackout filters on a per-minute session calendar
  (checked against the dataset calendar columns across DST); an inclusive daily sigma-hat band;
  the spread strictly below k x its causal New York hour-of-week median (overall median as a
  fallback, blocked without one). ADR 0052.
- SIGNAL-004: the signal engine (`xq.signals.engine.SignalEngine`) — a strategy defined entirely
  by YAML (`StrategySpec`, `experiments/configs/strategies/template_barrier.yaml`, a template
  for synthetic tests); every forecast of its model and target becomes a candidate with stop,
  target and time stop in sigma-hat units; uncalibrated, stale and filtered forecasts and
  unqualified EV are rejected with reasons; the best remaining candidate becomes the one
  `TradeIntent`; a `SignalRecord` for every candidate. Tested on hand-computed stops, costs and
  EV. ADR 0052.
- SIGNAL-005: the signal engine in the event backtester (`xq.backtest.strategies.SignalStrategy`)
  — forecasts made at each decision go through the signal engine, the risk engine and the
  simulated broker; every forecast and signal record is kept, and `SignalStrategy.audit` traces
  every fill through its order, risk decision and intent to its record and forecasts. A
  forecast-to-fill run on synthetic quotes with a causal stub forecaster (declared calibrated,
  not a model) traces every fill, and with the stub uncalibrated nothing reaches the risk engine.
  ADR 0052.
- VAL-003: probability of backtest overfitting by combinatorially symmetric cross-validation
  (`xq.validation.pbo.pbo_cscv`) — 16 contiguous blocks by default, C(16, 8) = 12,870 in-sample
  halves, the in-sample winner's out-of-sample logit rank, PBO, the probability of loss and the
  degradation slope. Proven on known truth (`tests/helpers/strategies.py`): a hand-computed case,
  noise families averaging 0.5, a graded genuine edge at or below the R2 limit, a single-point
  optimum on noise above it; registered in the recovery registry. ADR 0054.
- VAL-004: White's Reality Check, Hansen's SPA and the Romano–Wolf step-down across a strategy
  family (`xq.validation.spa.family_tests`) — one stationary bootstrap for all tests (Politis–White
  block, at least 5 periods), SPA's consistent p-value with its lower and upper bounds, Romano–Wolf
  adjusted p-values and survivors. Proven on known truth: about nominal size on noise-only families
  (iid and GARCH), a graded edge detected with its best configurations surviving, SPA's power over
  the Reality Check when poor strategies join the family. ADR 0054.
- VAL-006: multiple-testing control per test family (`xq.validation.multiple_testing`) — Holm
  (family-wise, the default), Benjamini–Hochberg (false discovery, only where a family declares
  it) and Bonferroni, adjusted within each family (`adjust_by_family`); the Sprint 6
  `holm_adjust` now delegates to it. Hand-computed references, agreement with statsmodels, and the
  controlled error rates on simulated nulls. ADR 0054.
- ROB-001: parameter perturbation and plateau metrics (`xq.robustness.perturb`) — each parameter
  moved by ±10/20/30 % (integers to the nearest integer at least one step away, gridded
  parameters to the neighbouring allowed values), one at a time, jointly and over pairwise heat-map
  grids; per level the joint neighbourhood's profitable share, median, worst and
  median-to-nominal ratio; the R2 `parameter_neighbourhood` check read from `config/gates.yaml`
  (`GatesConfig.criterion`, `GateCheck`). Proven on known truth: a single-point optimum on noise,
  chosen in sample, fails in about 85 % of replications; a genuine trend edge chosen the same way
  passes in at least 90 %. ADR 0054.
- ROB-002: cost and latency stress (`xq.robustness.costs_stress`) — the plan's grid one dimension
  at a time (spread x1.25/1.5/2 by widening quotes around the mid, slippage x2/3, latency
  +250 ms/1 s/5 s, financing x1.5 with credits reduced) and the R2 scenario from
  `config/gates.yaml` (1.5x spread and 2x slippage together), each screened again by
  `run_vectorized`; the break-even multiplier of all costs, found by the secant method on actual
  runs. Proven on known truth: the R2 scenario passes exactly when the gross edge covers the
  stressed costs, a thin edge profitable at baseline fails, the break-even run nets zero, spreads
  scale exactly, and latency eats a signal priced in over 10 s in proportion to the delay.
  ADR 0054.

### Changed

- C-22 (owner's decision, ADR 0053): position sizing scales on the edge per unit of risk instead
  of the raw calibrated probability — `ev_r = p_lcb x TP/SL - (1 - p_lcb) - round_trip_cost/SL`
  with `p_lcb` the probability's lower confidence bound, and the multiplier
  `clip(ev_r / ev_r_full, 0, 1)`; `ev_r_full` (0.25) and `lcb_z` (1.645) are provisional profile
  values (`risk-2`). The risk engine takes its own bound and prices the round trip with its own cost
  model (`CostModel.round_trip_cost_bps`, shared with the signal engine); an intent with a
  probability needs its standard error (`TradeIntent.p_se`) and a target. Tested: 1:1 and 2:1
  payoffs at break-even get no size, a 2:1 trade at p = 0.45 gets a positive one, costs and
  uncertainty shrink it, and the limit properties still hold; the forecast-to-fill run uses the
  default profile.

- EDA-006 holding periods are TGT-002's (ADR 0040, owner review of PR #9): candidates are TGT-002
  horizon labels; from every market-open decision of a 1m bar on a 5-minute grid, the move over h
  of market time is computed by `xq.targets.returns.compute` (mid) on quotes rebuilt from the 1m
  bars' closes, with `fwd_returns.v1`'s latency and fill delay, so trading-time horizons, the
  closed-market rule and `crosses_close` are the targets' own; n and the share of periods crossing
  a close are reported. `config/eda.yaml` gains `decision_step` and `target_set`.
- EDA-006 spread cost is half the closing spread of the 1m bar at the entry fill plus half that at
  the exit fill, each over its mid, instead of the holding bar's mean spread (ADR 0040).
- EDA-006 `xq research admit-horizons` refuses a list priced with placeholder costs unless
  `--allow-placeholder-costs` is passed; `config/horizons.yaml` records the cost basis, whether the
  costs were provisional, the flag and the source report and run (ADR 0040).
- EDA-006 analytic test: on a Gaussian random walk with known sigma and a constant spread, the mean
  absolute move over h matches sigma·sqrt(2h/π), the ratio matches spread ÷ that move, and
  admission flips at the 0.3 bound (ADR 0040).
- EDA-006 reports the median absolute move, the median cost and the median-based ratio beside the
  means (table, figure, admission list); admission stays on the mean ratio (ADR 0040).
- EDA-006 slippage sigma-hat without an earlier one-minute return: an expanding median of the
  sigma-hats of periods entered earlier, and the period is dropped when there is none; the
  whole-window median used data from after the entry (ADR 0040).
- EDA-006 per-session admission is report-only (ADR 0041): the admission list and
  `config/horizons.yaml` admit on the overall ratio only; the table's per-row flag is `below_bound`.
- EXP-002 accepts `trial_budget: 0` only for hypotheses of family `descriptive` (the H-0000
  prerequisite, ADR 0041, ADR 0042); every other family still needs a budget of at least one, and
  negative budgets are refused. H-0000 itself is not written or registered yet (C-16).
- VOL-006 selection Holm-adjusts the one-sided DM p-values of all challengers against the default
  before applying `dm_alpha` (owner review of PR #11): with twelve challengers equal to the default
  in truth, a simulated board promotes one in 4.5 % of 400 samples (30.5 % without the adjustment);
  `Selection.p_holm` records the adjusted p-values. ADR 0044.
- STAT-006 and VOL-005 trials are recorded in their own families, `linear_forecasts` and
  `volatility_models` (`xq.tracking.trials.MODEL_FAMILIES`), whatever the run's hypothesis, never
  in a trading-strategy family (owner review of PR #11); `arma_study` and `evaluate_forecasters`
  lose their `family_id` argument. A test records both under a `baselines` hypothesis and finds
  the `baselines` family's trial count, effective N and Sharpe variance unchanged. ADR 0046.
- EXP-002 refuses to register a hypothesis in a reserved family (ADR 0047): the model families
  `linear_forecasts` and `volatility_models` are listed once, in
  `xq.tracking.registry.RESERVED_FAMILIES`; both the hypothesis schema (`xq exp register`) and
  `registry.add_hypothesis_version` refuse them, so a trading-strategy hypothesis can no longer
  share a family with forecasting-model trials. The family constants move from
  `xq.tracking.trials` to `xq.tracking.registry`.
- BASE-005 H-0001 draft revised at the owner's request (ADR 0035, C-15), still unregistered: rule
  baselines are evaluated over the full pre-vault history after each rule's warm-up, with the
  fold-aligned version stored for comparison; they run on 1d and 1h signal bars (not 15m), so the
  trial budget is 36; the discovery and evaluation windows are "set from the real data's depth at
  registration" (registration is refused until then); descriptive slices by year and session.
- ARCH-007/ARCH-008 Docker verification (ADR 0032, C-6): `docker/Dockerfile` has `base`, `test`
  (dev dependencies, `git`, `tzdata`, the test suite; runs as the non-root user under
  `TZ=Asia/Tokyo`) and `runtime` (still the default and the compose target) stages; CI gains a
  `docker` job that builds and starts the runtime image, builds the test stage and runs the suite
  inside it.
- BT-001 costs stay provisional, and financing is a cost on both sides until broker terms replace
  it (ADR 0032): a `provisional: true` cost model must have strictly positive long and short
  financing rates (only a non-provisional model may credit a side). Net results carry the cost
  model's label — `SCREENING_LABEL`, "screening, placeholder costs", while it is provisional —
  as `CostModel.result_label` and `BacktestResult.cost_basis`, for every report to print.
- TGT-002 and BT-002: decisions taken while the market is closed (the 17:00 close itself, the
  daily break, weekends, holidays) get no target label and place no order (ADR 0032), instead of
  being entered at the reopen; decisions taken while open keep their label across a close with
  `crosses_close = true`. `MarketClock.is_open`; `BacktestResult.closed` lists the skipped
  decisions that would have traded (no entry, exit or change: the held position stays until the
  next decision taken while open); `forward_return` code version 4. The leakage suite checks that
  only open-market decisions are labelled.
- TGT-002 `1d` is one trading day (ADR 0032): a horizon label `<n>d` is n regular trading days of
  market time — 23 market hours for the 18:00–17:00 New York session
  (`xq.data.calendar.regular_trading_day`), so a `1d` label ends at the same session clock time one
  trading day later instead of an hour into the next session; `4h` stays 4 market hours. Labels may
  not mix days with other units (`1d6h` is refused). `TargetKind.expand` and `lookahead` take the
  trading-day length (`xq.targets.base.market_horizon`); `forward_return` code version 3 (dataset
  ids change); `_vol` targets scale `1d` by `sqrt(1380)` minutes.
- `docs/STATUS.md` tracks the current sprint and next task, review carry-overs with owners and
  closing commits, open owner decisions, provisional assumptions, known issues and per-phase
  status; `CLAUDE.md` gains a session protocol (read it after `CLAUDE.md`, keep it current, record
  chat decisions in an ADR and in it the same session, the repository wins over memory).
- DS-003 `resample_causal` respects availability (ADR 0026, C-7): a required keyword `latency`
  (how long after the end of its bin any member becomes available) labels each bin
  `bin end + latency`; tests with latency > 0 (hand-computed, a hypothesis property that
  truncates by availability, a counterexample showing the old labelling read a row two minutes
  early, and the leakage harness on bars published late).
- TGT-002 trading-time horizons (ADR 0026, C-5): `MarketClock` (`xq.data.calendar`) counts only
  market-open time from `config/sessions.yaml`; forward-return horizons and latency are measured
  on it, so decisions before a close or on a Friday are labelled over the break or weekend and
  decisions taken while closed are entered at the reopen. Targets gain a boolean `crosses_close`
  (a market close lies between entry and exit fills). Fill delays stay wall-clock. Target kinds
  take the clock, and their lookahead is market time plus wall time; the builder reads and gates
  quotes up to that reach. `forward_return` code version 2 (dataset ids change). Leakage suite:
  exits are checked against the market-time horizon; property tests for the clock.
- EXP-004 trial clustering (ADR 0026, C-4): trial returns are summed per trading day (17:00 New
  York roll) before they are correlated, and a pair needs 60 common trading days
  (`experiments.trial_clustering.min_common_days`, replacing `min_overlap: 20`); the correlation
  threshold stays 0.7. `daily_returns` is public for reports.
- DS-007 rollover window (ADR 0026, C-3): `in_rollover_window` is the New York clock window
  16:45–18:15 on every day (pre-close, daily break and reopen spread spike, Sunday reopen
  included) instead of ±15 minutes around the 17:00 anchor. `event_windows` accept clock windows
  (`tz`, `start`, `end`) beside anchored ones; the US release window is unchanged. Dataset ids
  change through the config digest.
- TGT-002 fill-delay diagnostic (ADR 0026, C-2): every target row records `fill_delay_s` (the
  later fill's delay after its intended time); the manifest (`fill_delays`) and
  `xq dataset build` report per target the labelled rows, those with a fill more than
  `datasets.fill_delay_report_s` (5 s) late and the largest delay. Values are unchanged.
- ADR 0026 records the owner's Sprint 3 review decisions: latency 1 s and fill delay 300 s kept
  (with a new fill-delay diagnostic), rollover window 16:45–18:15 New York, trial clustering on
  60 common trading days, trading-time horizons with a `crosses_close` flag, `ds_base.yaml` start
  and horizons kept, and a Docker re-check.
- ADR 0013 records the owner's decisions on the Sprint 2 open questions: quality thresholds
  ratified as provisional (one change allowed, by ADR, after the DQ-008 real-data review; never
  after a strategy result exists); hour-of-week spread buckets kept until DQ-008; exact duplicates
  stay flagged in the clean store and excluded from bars; vault-period validation belongs to
  GATE-002; the broker and calendar stay pending, so Sprint 2 stays "implemented and tested, not
  validated".
- `xq validate --include-vault` now refuses to run without `--i-understand-vault-access`
  (`validate_source(..., vault_access_confirmed=True)` for library callers) and logs a
  `vault_validation_access` warning naming the run, source and vault days on every use (ADR 0013).
- Dependencies pinned to `pandas>=2.2,<3` (the plan specifies pandas 2.x; pandas 3 changes datetime
  resolution inference) and `numpy<2.5` (numpy 2.5 raises deprecation errors inside pandas 2.3).
- `CLAUDE.md` no longer marks `docs/specs/project-instructions.md` as missing: the owner committed
  it together with the revised development plan (backlog tables back in section 9, STAT-008
  dependencies, Sprint 13 ordering, critical-path note).
- BT-009 reconciliation applies the 5 %-of-costs tolerance after the separately reported sizing
  effect (owner's decision, ADR 0050): a *sized* screen replays the screener's decisions with the
  event tier's lots where only sizing differs, `sizing_effect` = sized - screener and
  `within_tolerance` compares |event - sized| with the tolerance; event-only rules and their
  follow-ons stay inside the check, and the mechanical residual must still be below one cent.
  An exposure schedule on 100,000 USD now passes, its sizing effect alone above 5 % of costs.
- BT-005 limit orders (limit entries and take-profit legs) fill only when the price trades
  through the limit by at least one tick (`instrument.tick_size`): the bid a tick above a sell
  limit, the ask a tick below a buy limit, in tick mode and at a bar's open or over its range; a
  touch is not a fill, and the fill stays at the limit, never better. The bar-mode ambiguity
  check and the tick-mode ambiguous-bar diagnostic use the same rule (owner's decision, ADR 0050).
- RISK-005 replaces the Sprint 11 placeholder risk approver everywhere: `xq.risk.placeholder` is
  removed, `run_event_backtest` requires a `RiskEngine` (and takes an optional daily sigma-hat),
  the report's summary names the risk engine and its profile version, and the event-tier tests
  run through the real engine — the golden trade's short now carries a stop (its ledger chain
  gains the stop leg and its cancellation), the financing, constraint and reconciliation tests
  give their intents stops and, where halts would interrupt another mechanism under test, use the
  real engine with halts that cannot bind.

### Fixed

- TGT-002 forward returns: a quote while the market is closed (a stray quote in the daily break,
  within the fill delay) is never an entry or exit fill; the fill is the first quote at or after
  the intended time that lies in market hours, or there is no label, as in both backtest tiers.
  The forward-return code version goes from 4 to 5, which changes dataset ids (no real dataset
  exists; owner's go-ahead, ADR 0050, C-21). The screener and the targets share
  `MarketClock.first_open`.
- BT-002 screener: a quote while the market is closed (a stray quote in the daily break, for
  example) is never a fill quote; the fill is the first quote at or after the intended time that
  lies in market hours, or the trade is missed. Before, a closed-market quote within the fill
  delay could fill a decision taken just before the close. Found while reconciling the screener
  with the event tier, which never fills while closed (BT-005, BT-009).
- BT-002 screener: a position still open when the quotes end is charged the rollover that ends
  the last quote's trading day (marked at the last quote), as the event tier does; before,
  financing stopped at the last quote, understating the last day's costs of an open position.
  The financing series now lists every rollover from the first fill through that day's end.
  Found by the BT-009 reconciliation (the only mechanical residual between the tiers).
- ARCH-008 Docker test stage: the image now copies `docs/`, so the EXP-005 test that reads the
  committed research log (`docs/research/log.md`) passes inside it; the CI `docker` job had failed
  on `main` since Sprint 5 (PR #9) with `FileNotFoundError` for that file.
- WF-002: `run_walk_forward` no longer fails when a stitched metric is undefined (the hit rate of
  an all-zero forecast is NaN, which the non-null `metrics.value` column rejected with an
  `IntegrityError`); undefined metrics stay NaN in the result and are not logged, and
  `registry.log_metric` now refuses a non-finite value with a clear `ValueError`. Found by the
  BASE-005 board running the `zero_return` baseline.
- Dataset targets: a month of decisions whose only decision time is exactly `vault.start` no longer
  asks the catalog for an empty tick window (which it rejects); those decisions get no label,
  since every fill would need vault quotes.
- `asof_join` no longer fails with an `IndexError` when the right frame is empty (for example,
  context bars truncated before the first one is available); every row gets no match. Found by
  the DS-006 leakage harness.
- MT5 adapter: a file that mixes times with and without milliseconds is parsed row by row instead
  of being rejected (ADR 0004 already promised both forms).
- Ingest: a file whose SHA-256 is already stored under a *different* source is still skipped (the
  bytes are stored once) but now logs a `raw_file_already_ingested_under_other_source` warning
  naming both source ids, instead of skipping silently.
- DATA-007 `SPIKE` rule: the candidate is now the common move of bid and ask (one-sided spread
  widening, e.g. at the rollover, no longer looks like a spike) and its scale grows with the square
  root of the elapsed time (moves across pauses are judged on the pause). Cleaning code version 2;
  ADR 0008 supersedes the `SPIKE` definition in ADR 0006.
