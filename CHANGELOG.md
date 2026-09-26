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

### Changed

- Dependencies pinned to `pandas>=2.2,<3` (the plan specifies pandas 2.x; pandas 3 changes datetime
  resolution inference) and `numpy<2.5` (numpy 2.5 raises deprecation errors inside pandas 2.3).
