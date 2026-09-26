# Marshall — XAUUSD quantitative research platform (`xq`)

A research-grade platform for XAUUSD (spot gold) built around one question: does any candidate
strategy survive unseen data and realistic retail costs? The platform builds the validation
machinery (leakage-safe datasets, walk-forward evaluation, cost model, trial counting and evidence
gates) before any research is run, and treats a documented negative result as a valid outcome.

The full plan — 26 phases, the task backlog and the 16-sprint build order — is in
[`docs/specs/development-plan.md`](docs/specs/development-plan.md). The rules every change must
follow are in [`CLAUDE.md`](CLAUDE.md), and decisions are recorded in [`docs/adr/`](docs/adr/).

## Status

Sprint 1 (skeleton and raw ingestion) is complete: configuration, logging, CLI, CI, Docker, the
trading calendar, the metadata database, the MT5 source adapter with UTC normalization, and the
immutable raw store. See [`CHANGELOG.md`](CHANGELOG.md) for completed backlog tasks. Sprint 2
(clean ticks, bars and data quality) is next.

Open owner decisions (development plan, section 1): the execution broker and its data feed. The
source `mt5_primary` in `config/base.yaml` is a provisional placeholder (ADR 0004); no real market
data is in the repository.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync            # create the environment from uv.lock
uv run pytest      # run the test suite
```

Ingest the synthetic MT5 fixtures into the raw store (a second run is a no-op):

```bash
uv run xq ingest --source mt5_primary --path tests/fixtures/ticks/
uv run xq verify-raw                 # re-hash every raw file against the manifest
uv run xq config show                # resolved configuration, secrets masked
```

Or in Docker (research profile; `data/`, `logs/` and `reports/` are mounted from the host):

```bash
docker compose --profile research build
docker compose --profile research run --rm xq xq --version
```
