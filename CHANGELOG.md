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

### Changed

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

### Fixed

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
