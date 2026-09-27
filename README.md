# Marshall — XAUUSD quantitative research platform (`xq`)

A research-grade platform for XAUUSD (spot gold) built around one question: does any candidate
strategy survive unseen data and realistic retail costs? The platform builds the validation
machinery (leakage-safe datasets, walk-forward evaluation, cost model, trial counting and evidence
gates) before any research is run, and treats a documented negative result as a valid outcome.

The full plan — 26 phases, the task backlog and the 16-sprint build order — is in
[`docs/specs/development-plan.md`](docs/specs/development-plan.md). The rules every change must
follow are in [`CLAUDE.md`](CLAUDE.md), and decisions are recorded in [`docs/adr/`](docs/adr/).

## Status

Sprints 1 to 6, 11 and 12 A are merged; Sprint 9 (statistical validation and robustness, ADR 0051,
ADR 0054) is in review. Sprints 11 and 12 A ran ahead of Sprints 7-10 while real data is pending
(ADR 0048, ADR 0051). Sprint 2 (clean ticks, bars and data quality) is implemented and tested on
synthetic data but **not validated**: its quality report must first run on at least one year of real
broker ticks, followed by the human review (DQ-008); the owner's decisions on its open questions are
in ADR 0013. Sprint 3 (datasets, leakage harness, experiment registry, forward-return targets) and
Sprint 4 (walk-forward, cost model and screener, Sharpe inference, DSR, forecast comparison, the
evidence gates in `config/gates.yaml`, and the baseline board) are implemented and tested on
synthetic data only. Sprint 5 is build-only because no real broker data exists: the
exploratory-research report (distributions, dependence, seasonality, trend and reversion, cost to
volatility and horizon admission, all on the discovery window) and experiment conclusions are
implemented and tested on synthetic data and simulated processes; no EDA report has been generated
on real data and `config/horizons.yaml` does not exist yet. Sprint 6 (statistical and volatility
research) is build-only too: stationarity, dependence and variance-ratio tests, walk-forward ARMA
forecasts, the verdict-report framework, range and realized volatility estimators, EWMA/HAR/GARCH
forecasters, their evaluation on identical folds and the sigma-hat selection are implemented and
pass recovery tests on simulated processes; nothing has run on real data and no volatility model is
promoted. Sprint 11 (the event-driven backtester) is implemented and tested on synthetic data only:
event queue and clock, a broker simulator (brackets, gaps, pessimistic intrabar resolution, no fills
while closed), FIFO portfolio accounting, a decision ledger linking every order to its risk
decision, entry blackouts, reconciliation with the screener and a backtest report. Sprint 12 A adds
the risk engine (risk state rebuilt from the ledger, sizing, limits and halts, stop policy, kill
switch and data-health breakers; every order comes from a decision `RiskEngine.evaluate` issued,
with a provisional risk profile in `config/risk/default.yaml`) and the signal engine (schemas
exported to `docs/specs/interfaces/`, EV in sigma units, filters with a placeholder pass-through
regime filter, YAML-defined strategies, a record for every candidate, forecast to fill in the event
backtester), tested on synthetic data only; its review changed sizing to the edge per unit of risk
(ADR 0053). Sprint 9 adds the probability of backtest overfitting (CSCV), White's Reality Check,
Hansen's SPA and Romano-Wolf, Holm and Benjamini-Hochberg per test family, and the robustness
measures: parameter perturbation, cost and latency stress with the break-even multiplier,
block-bootstrap intervals and trade-order permutation, slicing read from the pre-registered
hypothesis and execution delay, each checked against `config/gates.yaml` where a gate applies, plus
`xq exp reproduce`. Every method is proven on simulated strategies with known truth (noise-only
families, a single-point optimum on noise, a genuine edge); none has run on real data. See
[`CHANGELOG.md`](CHANGELOG.md) for
completed backlog tasks and [`docs/STATUS.md`](docs/STATUS.md) for the current sprint, open
decisions and carry-over items.

Open owner decisions (development plan, section 1): the execution broker, its data feed and its
cost terms. The source `mt5_primary` in `config/base.yaml` is a provisional placeholder (ADR 0004),
the cost model is a provisional placeholder (every net result is "screening, placeholder costs"),
and no real market data is in the repository.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync            # create the environment from uv.lock
uv run pytest      # run the test suite
```

The data pipeline, shown on the synthetic MT5 fixtures (every step can be re-run safely):

```bash
uv run xq ingest --source mt5_primary --path tests/fixtures/ticks/   # immutable raw store
uv run xq clean --source mt5_primary          # flag bad ticks into versioned clean partitions
uv run xq build-bars --source mt5_primary     # bid/ask/mid bars on 7 timeframes
uv run xq spread-stats --source mt5_primary   # hour-of-week spread percentiles (pre-vault)
uv run xq validate --source mt5_primary       # data-quality checks (pre-vault) in reports/quality/
uv run xq dataset build experiments/configs/ds_base.yaml   # versioned dataset with targets
uv run xq exp register experiments/hypotheses/H-XXXX.yaml  # pre-register (copy TEMPLATE.yaml)
uv run xq exp trials                          # trial counts for multiple-testing corrections
uv run xq baselines run --dataset <ds-id>     # baseline board in reports/baselines/ (screening)
uv run xq research eda --dataset <ds-id> --hypothesis <H>  # EDA report on the discovery window
uv run xq exp close <experiment-id> --conclusion <yaml>    # close with a verdict (research log)
uv run xq exp audit                           # experiments still without a conclusion
uv run xq exp reproduce <run-id>              # rebuild the dataset, rerun, compare the metrics
uv run xq verify-raw                          # re-hash every raw file against the manifest
uv run xq config show                         # resolved configuration, secrets masked
```

The committed fixtures are sparse (one tick every ~90 s), so `xq validate` reports stale-quote and
missing-minute failures on them; that is the checks working, not a bug. For the same reason
`xq dataset build experiments/configs/ds_base.yaml` stops at the quality gate (DQ-007), listing
every failing fixture day and check: a meaningful base dataset needs real broker history for the
four years before the vault. The dataset, hypothesis, trial, baseline-board, EDA, conclusion and
reproduce commands are exercised end to end on dense synthetic weeks in `tests/integration/`. The
board's net figures are screening results while the cost model is a provisional placeholder
(ADR 0032).

Or in Docker (research profile; `data/`, `logs/` and `reports/` are mounted from the host):

```bash
docker compose --profile research build
docker compose --profile research run --rm xq xq --version
```
