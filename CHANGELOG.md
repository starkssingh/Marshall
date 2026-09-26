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

### Changed

- Dependencies pinned to `pandas>=2.2,<3` (the plan specifies pandas 2.x; pandas 3 changes datetime
  resolution inference) and `numpy<2.5` (numpy 2.5 raises deprecation errors inside pandas 2.3).
- `CLAUDE.md` no longer marks `docs/specs/project-instructions.md` as missing: the owner committed
  it together with the revised development plan (backlog tables back in section 9, STAT-008
  dependencies, Sprint 13 ordering, critical-path note).

### Fixed

- MT5 adapter: a file that mixes times with and without milliseconds is parsed row by row instead
  of being rejected (ADR 0004 already promised both forms).
- Ingest: a file whose SHA-256 is already stored under a *different* source is still skipped (the
  bytes are stored once) but now logs a `raw_file_already_ingested_under_other_source` warning
  naming both source ids, instead of skipping silently.
- DATA-007 `SPIKE` rule: the candidate is now the common move of bid and ask (one-sided spread
  widening, e.g. at the rollover, no longer looks like a spike) and its scale grows with the square
  root of the elapsed time (moves across pauses are judged on the pause). Cleaning code version 2;
  ADR 0008 supersedes the `SPIKE` definition in ADR 0006.
