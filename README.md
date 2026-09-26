# Marshall — XAUUSD quantitative research platform (`xq`)

A research-grade platform for XAUUSD (spot gold) built around one question: does any candidate
strategy survive unseen data and realistic retail costs? The platform builds the validation
machinery (leakage-safe datasets, walk-forward evaluation, cost model, trial counting and evidence
gates) before any research is run, and treats a documented negative result as a valid outcome.

The full plan — 26 phases, the task backlog and the 16-sprint build order — is in
[`docs/specs/development-plan.md`](docs/specs/development-plan.md). The rules every change must
follow are in [`CLAUDE.md`](CLAUDE.md), and decisions are recorded in [`docs/adr/`](docs/adr/).

## Status

Sprint 1 (skeleton and raw ingestion) is in progress. See [`CHANGELOG.md`](CHANGELOG.md) for
completed backlog tasks.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync            # create the environment from uv.lock
uv run pytest      # run the test suite
```

Or in Docker (research profile; `data/`, `logs/` and `reports/` are mounted from the host):

```bash
docker compose --profile research build
docker compose --profile research run --rm xq xq --version
```
