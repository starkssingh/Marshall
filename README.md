# Marshall — XAUUSD quantitative research platform (`xq`)

A research-grade platform for XAUUSD (spot gold) built around one question: does any candidate
strategy survive unseen data and realistic retail costs? The platform builds the validation
machinery (leakage-safe datasets, walk-forward evaluation, cost model, trial counting and evidence
gates) before any research is run, and treats a documented negative result as a valid outcome.

The full plan — 26 phases, the task backlog and the 16-sprint build order — is in
[`docs/specs/development-plan.md`](docs/specs/development-plan.md). The rules every change must
follow are in [`CLAUDE.md`](CLAUDE.md), and decisions are recorded in [`docs/adr/`](docs/adr/).

## Status

Sprints 1 to 6, 9, 11, 12 A, 12 B and 13, the Dukascopy data session and the C-15 session are
merged: the primary research feed is Dukascopy's XAUUSD bid/ask ticks (UTC, from 2003), downloaded
by the owner as dukascopy-node CSVs (ADR 0057, ADR 0062; see "Real data" below). In review: the
owner's decisions on C-26, C-27 and C-28 (ADR 0062: the CSV route, `ds_base` from 2015-01-01,
promotion to paper only on an event-tier R3, quieter third-party logs) and Sprint 7 part 1
(ADR 0063, below). The project is data-only
for now, with no execution venue. Sprints 11 and 12 A ran ahead of Sprints 7-10 while real data is
pending (ADR 0048, ADR 0051). Sprint 2 (clean ticks, bars and data quality) is implemented and
tested on synthetic data but **not validated**: its quality report must first run on at least one
year of real ticks, followed by the human review (DQ-008); the owner's decisions on its open
questions are in ADR 0013. Sprint 3 (datasets, leakage harness, experiment registry, forward-return
targets) and Sprint 4 (walk-forward, cost model and screener, Sharpe inference, DSR, forecast
comparison, the evidence gates in `config/gates.yaml`, and the baseline board) are implemented and
tested on synthetic data only. Sprint 5 is build-only because no real data exists: the
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
families, a single-point optimum on noise, a genuine edge); none has run on real data. Sprint 12 B
adds:

- a Monte Carlo that replays resampled trade outcomes through the real risk engine;
- noise injection;
- the robustness report and score against the gates;
- `xq validate-strategy <run_id>`, the combined significance and robustness report with R1 and R2
  verdicts, with an R1 test against the best baseline and an R2 decay test.

It is proven end to end on recorded simulated strategies (`xq robustness simulate`): a genuine
trend edge passes R1 and R2, and a single-point optimum on noise fails R2. Synthetic data only.
Sprint 13 adds the model registry and the release gates: content-hashed strategy bundles whose
status moves only on a recorded, passing gate result (enforced by the code and the database),
an active bundle per environment with rollback, a performance history, `xq gate evaluate` (R1
and R2 from a validation, the plan's ten gate items and a human-review template), and the
one-time vault evaluation (one token per validated bundle, ever; R3 recorded). It is tested end
to end on synthetic ticks against a test vault start; the vault has never been opened.
The baseline board now runs the revised H-0001 (C-15, ADR 0061): every rule on 1d and 1h signal
bars (`<name>@<timeframe>`, 36 strategies with the forecast-sign ones), evaluated over the full
pre-vault history from the dataset's first trading day (warmed up on quality-gated signal bars
from before the dataset's start, C-29, ADR 0064), with the fold-aligned view (the same returns on the
walk-forward test days) kept in `returns.parquet` for paired comparisons, validation and the
registry, and descriptive year and session slices. H-0001 is still unregistered: its windows
are set from the real data's depth at registration. Synthetic data only.
Sprint 7 part 1 (ADR 0063) adds the remaining targets — future realized volatility, MFE and MAE
in sigma units, triple-barrier labels (exact hit times on ticks; a bar touching both barriers is a
flagged stop), sign, big-move and trade/no-trade labels, label concurrency and average-uniqueness
weights — each passing the leakage harness, plus the monthly retraining schedule with a stitched
out-of-sample series checked for overlaps and gaps, and the walk-forward report with its decay
regression (`xq exp wf-report`). Synthetic data only; the features of Sprint 7 come next.
See
[`CHANGELOG.md`](CHANGELOG.md) for
completed backlog tasks and [`docs/STATUS.md`](docs/STATUS.md) for the current sprint, open
decisions and carry-over items.

Open owner decisions: the execution venue and its cost terms. No venue is chosen (ADR 0057), so
the cost model is a provisional placeholder (every net result is "screening, placeholder costs").
The primary source is `dukascopy` in `config/base.yaml`, and the MT5 source `mt5_primary` stays as
an optional adapter (ADR 0004). No real market data is in the repository.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync            # create the environment from uv.lock
uv run pytest      # run the test suite
```

The data pipeline, shown on the synthetic Dukascopy fixtures (every step can be re-run safely;
without `--source`, the pipeline reads `data.primary_source`, the Dukascopy source):

```bash
uv run xq ingest --path tests/fixtures/dukascopy/   # immutable raw store
uv run xq clean                               # flag bad ticks into versioned clean partitions
uv run xq build-bars                          # bid/ask/mid bars on 7 timeframes
uv run xq spread-stats                        # hour-of-week spread percentiles (pre-vault)
uv run xq validate                            # data-quality checks (pre-vault) in reports/quality/
uv run xq dataset build experiments/configs/ds_base.yaml   # versioned dataset with targets
uv run xq exp register experiments/hypotheses/H-XXXX.yaml  # pre-register (copy TEMPLATE.yaml)
uv run xq exp trials                          # trial counts for multiple-testing corrections
uv run xq baselines run --dataset <ds-id>     # baseline board in reports/baselines/ (screening;
                                              # needs a registered hypothesis, H-0001 by default)
uv run xq research eda --dataset <ds-id> --hypothesis <H>  # EDA report on the discovery window
uv run xq exp close <experiment-id> --conclusion <yaml>    # close with a verdict (research log)
uv run xq exp audit                           # experiments still without a conclusion
uv run xq exp reproduce <run-id>              # rebuild the dataset, rerun, compare the metrics
uv run xq exp wf-report <run-id> --strategy <name>  # walk-forward report: folds, decay (WF-005)
uv run xq robustness simulate --truth genuine --hypothesis <H>  # known-truth run (synthetic)
uv run xq validate-strategy <run-id>          # significance + robustness report vs the gates
uv run xq validate-strategy <board-run> --strategy <name>  # one board strategy, e.g. tsmom_252@1d
uv run xq registry register --run <board-run> --strategy <rule> --actor <you>  # hashed bundle
uv run xq gate evaluate <bundle>              # R1/R2 from a validation: gate report and review
uv run xq registry promote <bundle> --to candidate --actor <you> --reason <review>
uv run xq gate vault-token <bundle> --issued-by <you>  # validated bundles only, once ever
uv run xq gate vault-evaluate <bundle> --token <token> --quality-run <id>  # the vault run (R3)
uv run xq registry activate <bundle> --env paper --actor <you> --reason <why>  # or rollback
uv run xq verify-raw                          # re-hash every raw file against the manifest
uv run xq config show                         # resolved configuration, secrets masked
```

The committed fixtures are sparse (one tick every ~90 s), so `xq validate` reports stale-quote and
missing-minute failures on them; that is the checks working, not a bug. For the same reason
`xq dataset build experiments/configs/ds_base.yaml` stops at the quality gate (DQ-007), listing
every failing fixture day and check: a meaningful base dataset needs real history for the four
years before the vault (see "Real data" below). The MT5 fixtures in `tests/fixtures/ticks/` go
through the same steps with `--source mt5_primary`. The dataset, hypothesis, trial,
baseline-board, EDA, conclusion and reproduce commands are exercised end to end on dense
synthetic weeks in `tests/integration/`, and the simulate and validate-strategy commands on the
known-truth simulated strategies. The board's net figures are screening results while the cost
model is a provisional placeholder (ADR 0032).

Or in Docker (research profile; `data/`, `logs/` and `reports/` are mounted from the host):

```bash
docker compose --profile research build
docker compose --profile research run --rm xq xq --version
```

## Real data: Dukascopy XAUUSD ticks

The primary research feed is Dukascopy's XAUUSD bid/ask tick history (UTC timestamps, ticks
from 2003-05-05), source `dukascopy` in `config/base.yaml` (ADR 0057). The research sandbox has
no internet access, so the download runs on your machine, and real-data sessions run in Claude
Code there (ADR 0062). `data/` is git-ignored: market data is never committed.

**The working route: dukascopy-node CSVs, one per month** (ADR 0062). On 2026-10-04 the `.bi5`
endpoint answered HTTP 503, while dukascopy-node (Node.js, which now uses Dukascopy's JSON API)
exported March 2024 and the whole pipeline ran on it. One CSV per month, with Unix-millisecond
timestamps (the default) and volumes; `-to` is exclusive:

```bash
npx dukascopy-node -i xauusd -from 2024-03-01 -to 2024-04-01 -t tick -f csv -v \
  -bs 5 -bp 1000 -r 3 -re -fr -dir data/downloads/csv -fn XAUUSD_2024-03
```

`-re` retries hours that come back empty, and `-fr` keeps the export going once an hour's three
retries (`-r 3`) are spent: with `-re` alone the export aborts on the empty weekend hours. An hour
skipped that way is missing from the file; `xq validate`'s gap checks report it, and the month can
be exported again. The owner's loop over months (a partial file is deleted when an export fails,
`caffeinate` keeps the Mac awake, everything is logged) is in
[`docs/runbooks/real-data.md`](docs/runbooks/real-data.md). The `dukascopy` source reads these
CSVs (`timestamp,askPrice,bidPrice,askVolume,bidVolume`). Keep the default UTC offset
(`-utc 0`), and do not ingest both formats for the same period.

The base dataset `experiments/configs/ds_base.yaml` starts on 2015-01-01 (ADR 0062); download from
2014-01-01, so the year before it is there for warm-up and the quality report.

**The `.bi5` downloader**, `xq fetch dukascopy`, stays for the day the endpoint answers again:

```bash
uv run xq fetch dukascopy --instrument xauusd --from 2003-05-05 --to 2026-09-27 --out data/downloads
```

- **Output.** Under `data/`: one file per UTC hour with ticks, exactly the vendor's bytes
  (LZMA-compressed records), at
  `data/downloads/XAUUSD/<yyyy>/<mm>/<dd>/XAUUSD_<yyyy-mm-dd>_<HH>h_ticks.bi5`. Hours without
  ticks get no file.
- **Checksummed.** `data/downloads/XAUUSD/manifest.jsonl` has one line per hour: `ok` with the
  file's SHA-256, size and record count, or `empty`; the URL, HTTP status and fetch time. A
  payload is kept only if it decodes into whole records inside its hour.
- **Resumable and safe.** Stop it at any time (Ctrl-C) and run the same command again: hours in
  the manifest are verified against their SHA-256 and not requested again. A file is never
  overwritten or repaired; a mismatch stops the run and names the file. A lock file keeps a
  second download out of the same folder (delete `data/downloads/XAUUSD/.fetch.lock` only if no
  download is running).
- **Polite.** One request at a time, at most two a second, with retries and exponential backoff
  on errors (`sources.dukascopy.download` in `config/base.yaml`). The full history is about
  205,000 hourly requests, so expect more than a day in total; it can run in pieces, for example
  a year at a time, and the order does not matter.
- **Empty hours.** An hour inside market hours that comes back empty is asked again once, and it
  is recorded as empty only once a later hour brings ticks. A day or more of empty market hours
  in a row stops the run: the endpoint is more likely failing than the market silent.
  `--retry-empty` asks again for hours recorded empty.
- **`--to` must be before today (UTC)**; `--from` may not be earlier than 2003-05-05.

Then run the pipeline on the downloaded files (each step can be re-run safely):

```bash
uv run xq ingest --source dukascopy --path data/downloads/csv      # or data/downloads/XAUUSD (.bi5)
uv run xq clean --source dukascopy
uv run xq build-bars --source dukascopy
uv run xq spread-stats --source dukascopy
uv run xq validate --source dukascopy        # quality report in reports/quality/ (pre-vault)
```
