"""The `xq` command-line entry point (ARCH-006).

Global options select the configuration (``--profile``, ``--set``, ``--config-dir``); commands load
it lazily so ``xq --help`` and ``xq --version`` work without a config directory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal

import pandas as pd
import typer
import yaml
from pydantic import ValidationError
from sqlalchemy import Engine

import xq
from xq.core.config import (
    DEFAULT_CONFIG_DIR,
    AppConfig,
    config_as_dict,
    config_hash,
    load_config,
    parse_override,
)
from xq.core.errors import ConfigError, XQError
from xq.core.ids import git_sha, new_ulid
from xq.core.logging import configure_logging, shutdown_logging
from xq.core.time import ensure_utc
from xq.data.adapters.dukascopy_fetch import DayProgress, fetch_dukascopy
from xq.data.bars import build_bar_sets
from xq.data.clean import build_clean
from xq.data.raw_store import ingest, rebuild_mirror, verify_raw_store
from xq.data.spreads import build_spread_stats
from xq.datasets.builder import build_dataset, verify_dataset
from xq.datasets.spec import load_spec
from xq.datasets.vault import GateToken
from xq.models.board import load_board_config, run_baseline_board
from xq.quality.validate import validate_source
from xq.registry.bundles import (
    activate,
    activation_history,
    append_board_history,
    bundle_from_board_run,
    get_bundle,
    list_bundles,
    load_bundle,
    performance_history,
    register_bundle,
    rollback,
)
from xq.registry.evaluate import evaluate_bundle
from xq.registry.gates import list_gate_results, promote, retire
from xq.registry.models import Status, SubjectKind, status_history
from xq.registry.vault import issue_vault_token, run_vault_evaluation
from xq.research.eda.horizons import write_admission
from xq.research.eda.run import run_eda
from xq.robustness.simulated import SimulationSpec
from xq.tracking.conclusions import close_experiment, load_conclusion, unconcluded_experiments
from xq.tracking.db import current_revision, engine_for, head_revision, upgrade_to_head
from xq.tracking.hypotheses import register_hypothesis
from xq.tracking.registry import list_hypotheses
from xq.tracking.reproduce import (
    DEFAULT_ATOL,
    DEFAULT_RTOL,
    ReproductionStatus,
    describe,
    reproduce_run,
)
from xq.tracking.runs import experiment_run
from xq.tracking.trials import trial_count
from xq.validation.strategy import record_simulated_run, validate_run
from xq.validation.walkforward_report import board_report, write_report

EXIT_USAGE_ERROR = 2
#: `xq exp reproduce`: the rerun used other code, config or environment (never reproduced).
EXIT_DIFFERENT_CODE = 3

app = typer.Typer(
    name="xq",
    help="XAUUSD quantitative research platform.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


@dataclass(frozen=True)
class CliContext:
    """Global CLI options, resolved into an `AppConfig` on demand."""

    profile: str
    overrides: tuple[str, ...] = ()
    config_dir: Path | None = None

    @cached_property
    def config(self) -> AppConfig:
        """The resolved configuration, loaded on first access."""
        overrides = dict(parse_override(item) for item in self.overrides)
        return load_config(self.profile, overrides, config_dir=self.config_dir)


@contextmanager
def cli_errors() -> Iterator[None]:
    """Turn expected `XQError`s into a one-line message and exit code 2."""
    try:
        yield
    except XQError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE_ERROR) from exc


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"xq {xq.__version__}")
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    profile: Annotated[
        str,
        typer.Option("--profile", "-p", envvar="XQ_PROFILE", help="Configuration profile."),
    ] = "dev",
    overrides: Annotated[
        list[str] | None,
        typer.Option("--set", help="Override a config value: section.key=value (repeatable)."),
    ] = None,
    config_dir: Annotated[
        Path | None,
        typer.Option("--config-dir", envvar="XQ_CONFIG_DIR", help="Directory with YAML config."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """XAUUSD quantitative research platform."""
    ctx.obj = CliContext(profile=profile, overrides=tuple(overrides or ()), config_dir=config_dir)


config_app = typer.Typer(help="Inspect the resolved configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    fmt: Annotated[
        Literal["yaml", "json"], typer.Option("--format", help="Output format.")
    ] = "yaml",
) -> None:
    """Print the resolved configuration with secrets masked."""
    state: CliContext = ctx.obj
    with cli_errors():
        cfg = state.config
    data = config_as_dict(cfg)
    if fmt == "json":
        typer.echo(json.dumps({"config_hash": config_hash(cfg), "config": data}, indent=2))
    else:
        typer.echo(f"# profile: {cfg.profile}\n# config_hash: {config_hash(cfg)}")
        typer.echo(yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip())


@dataclass(frozen=True)
class PipelineRun:
    """What a pipeline command needs: config, a migrated metadata DB and run identifiers."""

    cfg: AppConfig
    engine: Engine
    run_id: str
    git_sha: str
    source_id: str | None = None

    @property
    def source(self) -> str:
        """The data source of a command that reads one: ``--source``, else the primary source."""
        if self.source_id is None:  # pragma: no cover - a programming error
            raise ConfigError("this command was not given a source")
        return self.source_id


@contextmanager
def pipeline_run(
    state: CliContext, *, source: str | None = None, reads_source: bool = False
) -> Iterator[PipelineRun]:
    """Load config, resolve and validate the source, bind run logging and migrate the DB.

    A command that `reads_source` uses `source`, or ``data.primary_source`` when it is None.
    """
    with cli_errors():
        cfg = state.config
        resolved = source if source is not None or not reads_source else cfg.primary_source()
        if resolved is not None:
            cfg.source(resolved)  # fail on an unknown source before touching anything
        run_id = new_ulid()
        sha = git_sha(cfg.paths.resolve(cfg.paths.root))
        configure_logging(cfg, run_id=run_id, git_sha=sha)
        engine = engine_for(cfg)
        try:
            upgrade_to_head(engine, cfg.paths.resolve(cfg.paths.migrations_dir))
            yield PipelineRun(cfg, engine, run_id, sha, resolved)
        finally:
            engine.dispose()
            shutdown_logging()


SourceOption = Annotated[
    str | None,
    typer.Option("--source", help="Configured source id [default: data.primary_source]."),
]
StartOption = Annotated[
    datetime | None, typer.Option("--start", formats=["%Y-%m-%d"], help="First trading day.")
]
EndOption = Annotated[
    datetime | None, typer.Option("--end", formats=["%Y-%m-%d"], help="Last trading day.")
]


@app.command("ingest")
def ingest_command(
    ctx: typer.Context,
    path: Annotated[Path, typer.Option("--path", help="File or directory of source files.")],
    source: SourceOption = None,
) -> None:
    """Copy source files into the immutable raw store with a Parquet mirror and manifest rows.

    Files already in the raw store (same SHA-256) are skipped, so re-running is a no-op.
    """
    with pipeline_run(ctx.obj, source=source, reads_source=True) as run:
        result = ingest(
            run.cfg, run.source, path, engine=run.engine, run_id=run.run_id, git_sha=run.git_sha
        )
    typer.echo(
        f"run {result.run_id}: ingested {len(result.ingested)} file(s) with {result.rows} rows; "
        f"skipped {len(result.skipped)} already in the raw store"
    )


@app.command("rebuild-mirror")
def rebuild_mirror_command(ctx: typer.Context, source: SourceOption = None) -> None:
    """Rewrite the Parquet mirror of a source's raw files from the stored copies."""
    with pipeline_run(ctx.obj, source=source, reads_source=True) as run:
        count = rebuild_mirror(run.cfg, run.engine, run.source)
    typer.echo(f"rebuilt the mirror of {count} raw file(s)")


@app.command("clean")
def clean_command(
    ctx: typer.Context,
    source: SourceOption = None,
    start: StartOption = None,
    end: EndOption = None,
    force: Annotated[bool, typer.Option("--force", help="Rebuild unchanged partitions.")] = False,
) -> None:
    """Flag (and, if configured, drop) bad ticks into versioned per-trading-day partitions."""
    with pipeline_run(ctx.obj, source=source, reads_source=True) as run:
        result = build_clean(
            run.cfg,
            run.engine,
            run.source,
            start=start.date() if start else None,
            end=end.date() if end else None,
            force=force,
        )
    typer.echo(
        f"rules {result.rules_version}: built {len(result.built)} trading day(s) with "
        f"{result.rows} ticks ({result.flagged} flagged, {result.dropped} dropped); "
        f"skipped {len(result.skipped)} unchanged"
    )


@app.command("build-bars")
def build_bars_command(
    ctx: typer.Context,
    source: SourceOption = None,
    start: StartOption = None,
    end: EndOption = None,
) -> None:
    """Build bid/ask/mid bars on all seven timeframes from the source's clean partitions."""
    with pipeline_run(ctx.obj, source=source, reads_source=True) as run:
        result = build_bar_sets(
            run.cfg,
            run.engine,
            run.source,
            start=start.date() if start else None,
            end=end.date() if end else None,
        )
    counts = ", ".join(f"{tf} {rows}" for tf, rows in result.rows.items())
    typer.echo(
        f"build {result.build_version} (clean rules {result.clean_rules_version}): "
        f"{len(result.months)} month(s); bars per timeframe: {counts}"
    )


@app.command("spread-stats")
def spread_stats_command(
    ctx: typer.Context,
    source: SourceOption = None,
    start: StartOption = None,
    end: EndOption = None,
) -> None:
    """Compute p50/p90/p99 spreads per New York hour of week (data before the vault only)."""
    with pipeline_run(ctx.obj, source=source, reads_source=True) as run:
        result = build_spread_stats(
            run.cfg,
            run.engine,
            run.source,
            start=start.date() if start else None,
            end=end.date() if end else None,
        )
    typer.echo(
        f"spread statistics for {result.hours} hour(s) of week from {result.ticks} ticks, "
        f"{result.computed_from} to {result.computed_to}"
    )


@app.command("validate")
def validate_command(
    ctx: typer.Context,
    source: SourceOption = None,
    start: StartOption = None,
    end: EndOption = None,
    include_vault: Annotated[
        bool,
        typer.Option(
            "--include-vault",
            help="Also validate vault days. Release-gate procedure only (ADR 0013); needs "
            "--i-understand-vault-access, is logged and is recorded on the run.",
        ),
    ] = False,
    vault_access_confirmed: Annotated[
        bool,
        typer.Option(
            "--i-understand-vault-access",
            help="Confirm that --include-vault reads the vault (the untouchable holdout).",
        ),
    ] = False,
) -> None:
    """Grade every data-quality check per trading day and write a report."""
    with pipeline_run(ctx.obj, source=source, reads_source=True) as run:
        result = validate_source(
            run.cfg,
            run.engine,
            run.source,
            run_id=run.run_id,
            git_sha=run.git_sha,
            start=start.date() if start else None,
            end=end.date() if end else None,
            include_vault=include_vault,
            vault_access_confirmed=vault_access_confirmed,
        )
    totals = result.summary["totals"]
    vault_note = "; includes vault days" if include_vault else ""
    typer.echo(
        f"quality run {result.run_id}: {len(result.days)} trading day(s); "
        f"{totals['pass']} pass, {totals['warn']} warn, {totals['fail']} fail{vault_note}"
    )
    for check_id, row in result.summary["checks"].items():
        if row["warn"] or row["fail"]:
            typer.echo(f"  {check_id}: {row['warn']} warn, {row['fail']} fail")
    typer.echo(f"report: {result.report_path}")


@app.command("verify-raw")
def verify_raw_command(ctx: typer.Context) -> None:
    """Re-hash every raw-store file against the manifest; exit 1 if any differ."""
    state: CliContext = ctx.obj
    with cli_errors():
        cfg = state.config
    engine = engine_for(cfg)
    try:
        problems = verify_raw_store(cfg, engine)
    finally:
        engine.dispose()
    for problem in problems:
        typer.echo(f"{problem.raw_file_id}\t{problem.problem}\t{problem.path}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("raw store verified: every file matches its manifest entry")


fetch_app = typer.Typer(
    help="Download vendor data to local files (run on a machine with internet access).",
    no_args_is_help=True,
)
app.add_typer(fetch_app, name="fetch")


@fetch_app.command("dukascopy")
def fetch_dukascopy_command(
    ctx: typer.Context,
    instrument: Annotated[str, typer.Option("--instrument", help="Instrument id, e.g. xauusd.")],
    first: Annotated[datetime, typer.Option("--from", formats=["%Y-%m-%d"], help="First UTC day.")],
    last: Annotated[
        datetime,
        typer.Option("--to", formats=["%Y-%m-%d"], help="Last UTC day (inclusive; before today)."),
    ],
    out: Annotated[
        Path, typer.Option("--out", help="Output directory; files go below <out>/<SYMBOL>/.")
    ],
    source: Annotated[
        str, typer.Option("--source", help="Configured Dukascopy source.")
    ] = "dukascopy",
    retry_empty: Annotated[
        bool, typer.Option("--retry-empty", help="Ask again for hours recorded empty.")
    ] = False,
) -> None:
    """Download Dukascopy hourly tick files (.bi5) with a SHA-256 manifest (ADR 0057).

    One polite request at a time; resumable (hours in <out>/<SYMBOL>/manifest.jsonl are verified,
    not fetched again); never overwrites a file. Then:
    xq ingest --source dukascopy --path <out>/<SYMBOL>"""
    state: CliContext = ctx.obj
    with cli_errors():
        cfg = state.config
        declared = cfg.source(source)
        if declared.instrument != instrument.lower():
            raise ConfigError(
                f"source {source!r} is declared for {declared.instrument!r}, not {instrument!r}"
            )
        configure_logging(
            cfg, run_id=new_ulid(), git_sha=git_sha(cfg.paths.resolve(cfg.paths.root))
        )
        try:
            result = fetch_dukascopy(
                cfg,
                source,
                first.date(),
                last.date(),
                out,
                retry_empty=retry_empty,
                on_day=_echo_day,
            )
        finally:
            shutdown_logging()
    typer.echo(
        f"{result.fetched} hour(s) fetched, {result.empty} empty "
        f"({result.empty_in_market} inside market hours), "
        f"{result.already_present + result.adopted} already present; "
        f"{result.requests} request(s); files under {result.folder}; manifest {result.manifest}"
    )
    if result.unconfirmed_empty:
        typer.echo(
            f"warning: the last {result.unconfirmed_empty} hour(s) inside market hours came back "
            "empty with no later hour to confirm the feed was answering; they are not recorded "
            "and the next run asks for them again"
        )
    if result.empty_in_market:
        typer.echo(
            f"note: {result.empty_in_market} empty hour(s) inside the calendar's market hours "
            "(holidays or vendor gaps); --retry-empty asks for them again"
        )
    typer.echo(f"next: xq ingest --source {source} --path {result.folder}")


def _echo_day(progress: DayProgress) -> None:
    typer.echo(
        f"{progress.day}: {progress.fetched} fetched, {progress.empty} empty, "
        f"{progress.already_present} already present"
    )


dataset_app = typer.Typer(
    help="Build and inspect versioned datasets (DS-001..007).", no_args_is_help=True
)
app.add_typer(dataset_app, name="dataset")


@dataset_app.command("build")
def dataset_build(
    ctx: typer.Context,
    spec_path: Annotated[Path, typer.Argument(help="Dataset spec YAML.")],
) -> None:
    """Materialize a dataset spec under data/datasets/<dataset_id>/ (a rebuild is verified)."""
    with pipeline_run(ctx.obj) as run:
        ref = build_dataset(run.cfg, run.engine, load_spec(spec_path), git_sha=run.git_sha)
    manifest = ref.manifest
    action = "reproduced (identical content)" if ref.reproduced else "built"
    typer.echo(
        f"dataset {ref.dataset_id} {action}: {manifest['row_count']} rows, "
        f"{manifest['decision_time_first']} to {manifest['decision_time_last']}; "
        f"sha256 {manifest['sha256'][:16]}"
    )
    if "fill_delays" in manifest:
        delays = manifest["fill_delays"]
        typer.echo(f"labels with a fill more than {delays['threshold_s']:g} s late:")
        for name, row in delays["targets"].items():
            largest = "n/a" if row["max_delay_s"] is None else f"{row['max_delay_s']:g} s"
            typer.echo(f"  {name}: {row['delayed']} of {row['labelled']} labelled (max {largest})")
    typer.echo(f"path: {ref.path}")


@dataset_app.command("show")
def dataset_show(
    ctx: typer.Context,
    dataset_id: Annotated[str, typer.Argument(help="Dataset id (ds-...).")],
) -> None:
    """Verify a dataset's files and print its manifest."""
    state: CliContext = ctx.obj
    with cli_errors():
        manifest = verify_dataset(state.config, dataset_id)
    typer.echo(json.dumps(manifest, indent=2))


exp_app = typer.Typer(
    help="Hypotheses, experiment runs, trial counts, conclusions and reproduction (EXP-001..006).",
    no_args_is_help=True,
)
app.add_typer(exp_app, name="exp")


@exp_app.command("register")
def exp_register(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Hypothesis file (H-XXXX.yaml).")],
) -> None:
    """Pre-register a hypothesis; an edited file becomes a new, visible version."""
    with pipeline_run(ctx.obj) as run:
        before = {(h.hypothesis_id, h.version) for h in list_hypotheses(run.engine)}
        ref = register_hypothesis(run.cfg, run.engine, path)
    state = "unchanged" if (ref.hypothesis_id, ref.version) in before else "registered"
    typer.echo(
        f"{ref.hypothesis_id} version {ref.version} {state} "
        f"(family {ref.family_id}, sha256 {ref.yaml_hash[:16]})"
    )


@exp_app.command("hypotheses")
def exp_hypotheses(ctx: typer.Context) -> None:
    """List every registered hypothesis version."""
    with pipeline_run(ctx.obj) as run:
        rows = list_hypotheses(run.engine)
    for h in rows:
        typer.echo(f"{h.hypothesis_id}\tv{h.version}\t{h.status}\t{h.family_id}\t{h.title}")


@exp_app.command("trials")
def exp_trials(
    ctx: typer.Context,
    family: Annotated[
        str | None, typer.Option("--family", help="Trial family (default: all families).")
    ] = None,
) -> None:
    """Show trial counts and the effective number of independent trials."""
    with pipeline_run(ctx.obj) as run:
        stats = trial_count(run.cfg, run.engine, family)
    variance = "n/a" if stats.sharpe_variance is None else f"{stats.sharpe_variance:.4g}"
    typer.echo(
        f"family {stats.family_id or 'all'}: {stats.n_trials} trial(s), "
        f"{stats.n_test_evaluations} evaluated on test folds, "
        f"{stats.effective_n} effectively independent; Sharpe variance {variance}"
    )


@exp_app.command("close")
def exp_close(
    ctx: typer.Context,
    experiment_id: Annotated[str, typer.Argument(help="Experiment id.")],
    conclusion_path: Annotated[
        Path,
        typer.Option(
            "--conclusion",
            help="Conclusion YAML (verdict and the five fields; experiments/conclusions/).",
        ),
    ],
) -> None:
    """Close an experiment with its conclusion and append it to the research log (EXP-005)."""
    with pipeline_run(ctx.obj) as run:
        record = close_experiment(
            run.cfg, run.engine, experiment_id, load_conclusion(conclusion_path)
        )
        log = run.cfg.paths.resolve(run.cfg.paths.research_log)
    typer.echo(f"experiment {experiment_id} closed: {record.verdict}; research log {log}")


@exp_app.command("audit")
def exp_audit(ctx: typer.Context) -> None:
    """List experiments without a conclusion; exit 1 if any (none may remain at a sprint end)."""
    with pipeline_run(ctx.obj) as run:
        open_experiments = unconcluded_experiments(run.engine)
    for e in open_experiments:
        typer.echo(f"{e.experiment_id}\t{e.hypothesis_id} v{e.hypothesis_version}\t{e.title}")
    typer.echo(f"{len(open_experiments)} experiment(s) without a conclusion")
    if open_experiments:
        raise typer.Exit(1)


@exp_app.command("reproduce")
def exp_reproduce(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Id of the finished run to reproduce.")],
    rtol: Annotated[
        float, typer.Option("--rtol", help="Relative tolerance of each metric.")
    ] = DEFAULT_RTOL,
    atol: Annotated[
        float, typer.Option("--atol", help="Absolute tolerance of each metric.")
    ] = DEFAULT_ATOL,
    exploratory: Annotated[
        bool,
        typer.Option(
            "--exploratory",
            help="Allow a dirty git tree; the reproduction is then not confirmatory.",
        ),
    ] = False,
) -> None:
    """Rebuild a run's dataset, repeat the run and compare its metrics (EXP-006).

    Exit 0 only for REPRODUCED (same git sha, config hash and lock hash, every judged metric
    within tolerance); 1 for NOT_REPRODUCED; 3 for RERUN_DIFFERENT_CODE (C-24, ADR 0055)."""
    with pipeline_run(ctx.obj) as run:
        result = reproduce_run(
            run.cfg, run.engine, run_id, rtol=rtol, atol=atol, exploratory=exploratory
        )
    for line in describe(result):
        typer.echo(line)
    if result.status is ReproductionStatus.NOT_REPRODUCED:
        raise typer.Exit(1)
    if result.status is ReproductionStatus.RERUN_DIFFERENT_CODE:
        raise typer.Exit(EXIT_DIFFERENT_CODE)


@exp_app.command("wf-report")
def exp_wf_report(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Id of a finished baseline board run.")],
    strategy: Annotated[str, typer.Option("--strategy", help="Board strategy to report.")],
) -> None:
    """Walk-forward report of one board strategy: per-fold metrics, the fold Sharpe
    distribution and the decay regression (WF-005). Reads the run's stored returns and folds;
    records no run and no trial."""
    with pipeline_run(ctx.obj) as run:
        try:
            report = board_report(
                run.engine,
                run_id,
                strategy,
                periods_per_year=run.cfg.backtest_config().periods_per_year,
            )
        except KeyError as exc:
            typer.echo(f"error: {exc.args[0]}", err=True)
            raise typer.Exit(2) from exc
        directory = run.cfg.paths.resolve(run.cfg.paths.reports_dir) / "walkforward" / run_id
        md, js = write_report(report, directory)
    d = report.distribution
    typer.echo(f"{strategy}: {int(d['n_folds'])} folds, median fold Sharpe {d['median']:.2f}")
    if report.decay is not None:
        typer.echo(
            f"decay: one-sided p = {report.decay.p_value:.4f} ({report.decay.n_folds} folds)"
        )
    else:
        typer.echo(f"decay: not computed ({report.decay_note})")
    typer.echo(f"report: {md}")
    typer.echo(f"json: {js}")


baselines_app = typer.Typer(
    help="Walk-forward baseline board (BASE-001, BASE-002, BASE-005).", no_args_is_help=True
)
app.add_typer(baselines_app, name="baselines")
DEFAULT_BOARD = Path("experiments/configs/baselines/board.yaml")


@baselines_app.command("run")
def baselines_run(
    ctx: typer.Context,
    dataset: Annotated[str, typer.Option("--dataset", help="Dataset id (ds-...).")],
    targets: Annotated[
        list[str] | None,
        typer.Option("--target", help="Target to forecast (repeatable; default: the board's)."),
    ] = None,
    board_path: Annotated[
        Path, typer.Option("--config", help="Board configuration YAML.")
    ] = DEFAULT_BOARD,
    hypothesis: Annotated[
        str, typer.Option("--hypothesis", help="Registered hypothesis the run belongs to.")
    ] = "H-0001",
    seed: Annotated[int, typer.Option("--seed", help="Run seed.")] = 0,
    exploratory: Annotated[
        bool,
        typer.Option(
            "--exploratory", help="Allow a dirty git tree; the run is then not confirmatory."
        ),
    ] = False,
    jobs: Annotated[int, typer.Option("--jobs", help="Processes for walk-forward folds.")] = 1,
) -> None:
    """Put every baseline through walk-forward and the cost model; write the board report.

    Net results are screening results while the cost model is provisional; the report marks them.
    """
    with pipeline_run(ctx.obj) as run:
        board = load_board_config(board_path)
        chosen = list(targets or board.targets)
        with experiment_run(
            run.cfg,
            run.engine,
            hypothesis,
            {"board": board.model_dump(mode="json"), "targets": chosen},
            kind="baseline_board",
            seed=seed,
            dataset_id=dataset,
            exploratory=exploratory,
        ) as context:
            result = run_baseline_board(context, dataset, board, targets=chosen, n_jobs=jobs)
    summary = result.summary
    typer.echo(
        f"baseline board of {dataset} ({result.cost_basis}): {len(result.strategies)} "
        f"strategies, {len(result.forecasts)} forecast baselines; out of sample "
        f"{summary['oos_start']} to {summary['oos_end']} ({summary['oos_days']} trading days); "
        f"trials {summary['trials']['raw']} raw, {summary['trials']['effective']} effective"
    )
    for row in result.strategies.to_dict(orient="records"):
        typer.echo(
            f"  {row['strategy']}: Sharpe {row['sharpe']:.2f} "
            f"[{row['sharpe_ci_low']:.2f}, {row['sharpe_ci_high']:.2f}], "
            f"p {row['sharpe_p']:.3f}, DSR {row['dsr']:.3f}, {row['trade_count']:.0f} trades "
            f"({row['cost_basis']})"
        )
    typer.echo(f"report: {result.report_dir}")


research_app = typer.Typer(
    help="Exploratory research on the discovery window (EDA-001..006).", no_args_is_help=True
)
app.add_typer(research_app, name="research")


@research_app.command("eda")
def research_eda(
    ctx: typer.Context,
    dataset: Annotated[str, typer.Option("--dataset", help="Dataset id (ds-...).")],
    hypothesis: Annotated[
        str, typer.Option("--hypothesis", help="Registered hypothesis the run belongs to.")
    ],
    seed: Annotated[int, typer.Option("--seed", help="Run seed (bootstrap resamples).")] = 0,
    end: Annotated[
        str | None,
        typer.Option(
            "--end",
            help="Stop before the discovery window ends (tz-aware ISO instant); never after it.",
        ),
    ] = None,
    exploratory: Annotated[
        bool,
        typer.Option(
            "--exploratory", help="Allow a dirty git tree; the run is then not confirmatory."
        ),
    ] = False,
) -> None:
    """Write the EDA report of a dataset's discovery window (distributions, dependence,
    seasonality, trend, cost to volatility and the horizon admission list)."""
    with pipeline_run(ctx.obj) as run:
        stop = ensure_utc(pd.Timestamp(end)) if end is not None else None
        with experiment_run(
            run.cfg,
            run.engine,
            hypothesis,
            {"eda": run.cfg.eda_config().model_dump(mode="json"), "end": str(stop)},
            kind="eda",
            seed=seed,
            dataset_id=dataset,
            exploratory=exploratory,
        ) as context:
            result = run_eda(context, dataset, end=stop)
    admission = result.admission
    typer.echo(
        f"EDA of {dataset} on the discovery window {result.window.start} to {result.window.end}; "
        f"horizons admitted ({admission.cost_basis}): {', '.join(admission.admitted) or 'none'}; "
        f"excluded: {', '.join(admission.excluded) or 'none'}"
    )
    typer.echo(f"report: {result.report_dir}")


@research_app.command("admit-horizons")
def research_admit_horizons(
    ctx: typer.Context,
    report: Annotated[Path, typer.Option("--report", help="EDA report directory.")],
    allow_placeholder_costs: Annotated[
        bool,
        typer.Option(
            "--allow-placeholder-costs",
            help="Write a list priced with provisional (placeholder) costs; recorded in the file.",
        ),
    ] = False,
) -> None:
    """Copy a confirmatory EDA report's horizon admission list to config/horizons.yaml.

    Refused while the report's costs are placeholders, unless --allow-placeholder-costs is given.
    """
    state: CliContext = ctx.obj
    with cli_errors():
        state.config  # noqa: B018 - validate the configuration directory before writing into it
        target = write_admission(
            report,
            state.config_dir or DEFAULT_CONFIG_DIR,
            allow_placeholder_costs=allow_placeholder_costs,
        )
    typer.echo(f"horizon admission list written to {target}")


db_app = typer.Typer(help="Metadata database migrations.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade(ctx: typer.Context) -> None:
    """Apply pending migrations to the configured metadata database."""
    state: CliContext = ctx.obj
    with cli_errors():
        cfg = state.config
    engine = engine_for(cfg)
    try:
        upgrade_to_head(engine, cfg.paths.resolve(cfg.paths.migrations_dir))
        typer.echo(f"metadata database at revision {current_revision(engine)}")
    finally:
        engine.dispose()


@db_app.command("current")
def db_current(ctx: typer.Context) -> None:
    """Show the metadata database's migration revision and the newest available one."""
    state: CliContext = ctx.obj
    with cli_errors():
        cfg = state.config
    engine = engine_for(cfg)
    try:
        current = current_revision(engine) or "none"
        head = head_revision(cfg.paths.resolve(cfg.paths.migrations_dir)) or "none"
        typer.echo(f"current: {current}\nhead: {head}")
    finally:
        engine.dispose()


robustness_app = typer.Typer(
    help="Robustness research (ROB-001 ... ROB-008) and known-truth simulated strategies.",
    no_args_is_help=True,
)
app.add_typer(robustness_app, name="robustness")


@robustness_app.command("simulate")
def robustness_simulate(
    ctx: typer.Context,
    truth: Annotated[
        str, typer.Option("--truth", help="genuine (a real trend edge) or overfit (noise).")
    ],
    hypothesis: Annotated[
        str, typer.Option("--hypothesis", help="Registered hypothesis the run belongs to.")
    ],
    seed: Annotated[int, typer.Option("--seed", help="Seed of the simulation.")] = 0,
) -> None:
    """Record a known-truth simulated strategy as a run (synthetic, always exploratory).

    Its family's configurations are recorded as trials of the hypothesis's family. It exists to
    prove `xq validate-strategy`; it is never evidence."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        try:
            spec = SimulationSpec(truth=truth, seed=seed)  # type: ignore[arg-type]
        except ValidationError as exc:
            raise ConfigError(f"invalid simulation: {exc}") from exc
        ref = record_simulated_run(run.cfg, run.engine, spec, hypothesis_id=hypothesis)
    typer.echo(f"run {ref.run_id}: simulated {truth} strategy, seed {seed} (synthetic)")


@app.command("validate-strategy")
def validate_strategy_command(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Id of the finished run that chose the strategy.")],
    strategy: Annotated[
        str | None,
        typer.Option("--strategy", help="The strategy to validate when the run holds several."),
    ] = None,
    exploratory: Annotated[
        bool,
        typer.Option(
            "--exploratory",
            help="Allow a dirty git tree; the validation run is then not confirmatory.",
        ),
    ] = False,
) -> None:
    """Significance and robustness report of a recorded strategy against config/gates.yaml.

    Writes report.md and report.json under reports/validation/<run_id>/<validation run>/ and
    records every test (stat_tests) and robustness measure (robustness_results). Exit 0 when
    the report is produced, whatever its verdicts."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        outcome = validate_run(
            run.cfg, run.engine, run_id, strategy=strategy, exploratory=exploratory
        )
    for line in outcome.validation.summary_lines():
        typer.echo(line)
    typer.echo(f"validation run {outcome.run.run_id}; report: {outcome.report_dir / 'report.md'}")


registry_app = typer.Typer(
    help="Strategy bundles and their status (MREG-001 ... MREG-005).", no_args_is_help=True
)
app.add_typer(registry_app, name="registry")
ActorOption = Annotated[str, typer.Option("--actor", help="Who makes the change (recorded).")]
ReasonOption = Annotated[str, typer.Option("--reason", help="Why (recorded).")]


@registry_app.command("register")
def registry_register(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Option("--run", help="Baseline board run the strategy is from.")],
    strategy: Annotated[str, typer.Option("--strategy", help="The board's rule to bundle.")],
    actor: ActorOption,
    name: Annotated[str | None, typer.Option("--name", help="Display name.")] = None,
) -> None:
    """Bundle a rule baseline of a board run (content-hashed; idempotent; status draft); its
    out-of-sample screen becomes the bundle's backtest history (MREG-004)."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        content = bundle_from_board_run(run.cfg, run.engine, run_id, strategy)
        ref = register_bundle(
            run.engine,
            content,
            name=name or strategy,
            origin_run_id=run_id,
            origin_strategy=strategy,
            actor=actor,
        )
        days = append_board_history(run.cfg, run.engine, ref.bundle_id)
    typer.echo(f"bundle {ref.bundle_id} ({ref.name}): {ref.status}")
    if days:
        typer.echo(f"backtest history: {days} trading days from run {run_id}")


@registry_app.command("list")
def registry_list(ctx: typer.Context) -> None:
    """Every bundle with its status."""
    with pipeline_run(ctx.obj) as run:
        bundles = list_bundles(run.engine)
    for ref in bundles:
        origin = f"{ref.origin_run_id}:{ref.origin_strategy}" if ref.origin_run_id else "-"
        typer.echo(f"{ref.short_id}\t{ref.status}\t{ref.name}\t{origin}")


@registry_app.command("show")
def registry_show(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
) -> None:
    """A bundle's content (checked against its id), gate results and status history."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        content = load_bundle(run.engine, bundle_id)
        ref = get_bundle(run.engine, bundle_id)
        results = list_gate_results(run.engine, SubjectKind.BUNDLE, ref.bundle_id)
        history = status_history(run.engine, SubjectKind.BUNDLE, ref.bundle_id)
    typer.echo(f"bundle {ref.bundle_id} ({ref.name}): {ref.status}")
    typer.echo(f"origin: {ref.origin_run_id or '-'} {ref.origin_strategy or ''}".rstrip())
    typer.echo(json.dumps(content.model_dump(mode="json"), indent=2, sort_keys=True))
    for result in results:
        typer.echo(f"gate: {result.describe()}")
    for change in history:
        typer.echo(
            f"status: {change.from_status or '-'} -> {change.to_status} by {change.actor} "
            f"({change.reason}) at {change.changed_at}"
        )


@registry_app.command("promote")
def registry_promote(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
    to: Annotated[str, typer.Option("--to", help="The next status.")],
    actor: ActorOption,
    reason: ReasonOption,
) -> None:
    """Promote a bundle one step; needs a passing latest result of the matching gate."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        try:
            target = Status(to)
        except ValueError as exc:
            raise ConfigError(f"unknown status {to!r}") from exc
        ref = get_bundle(run.engine, bundle_id)
        change = promote(
            run.engine, SubjectKind.BUNDLE, ref.bundle_id, target, actor=actor, reason=reason
        )
    typer.echo(
        f"bundle {ref.short_id}: {change.from_status} -> {change.to_status} "
        f"(gate result {change.gate_result_id})"
    )


@registry_app.command("retire")
def registry_retire(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
    actor: ActorOption,
    reason: ReasonOption,
) -> None:
    """Retire a bundle (no gate needed)."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        ref = get_bundle(run.engine, bundle_id)
        change = retire(run.engine, SubjectKind.BUNDLE, ref.bundle_id, actor=actor, reason=reason)
    typer.echo(f"bundle {ref.short_id}: {change.from_status} -> {change.to_status}")


@registry_app.command("history")
def registry_history(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
) -> None:
    """A bundle's daily performance per source (backtest, vault, paper, live)."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        history = performance_history(run.engine, bundle_id)
    if history.empty:
        typer.echo("no performance recorded")
        return
    for source, rows in history.groupby("source", sort=False):
        typer.echo(
            f"{source}: {len(rows)} trading days, {rows['trading_day'].iloc[0]} to "
            f"{rows['trading_day'].iloc[-1]}, net return {rows['net_return'].sum():.4%}"
        )


EnvOption = Annotated[str, typer.Option("--env", help="Environment: paper or prod.")]


@registry_app.command("activate")
def registry_activate(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
    environment: EnvOption,
    actor: ActorOption,
    reason: ReasonOption,
) -> None:
    """Point an environment at a bundle its status allows (paper: paper or beyond)."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        change = activate(run.engine, environment, bundle_id, actor=actor, reason=reason)
    previous = change.previous_bundle_id[:12] if change.previous_bundle_id else "none"
    typer.echo(f"{environment}: {change.bundle_id} active (was {previous})")


@registry_app.command("rollback")
def registry_rollback(
    ctx: typer.Context, environment: EnvOption, actor: ActorOption, reason: ReasonOption
) -> None:
    """Restore the bundle active in an environment before the current one (the same hash)."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        change = rollback(run.engine, environment, actor=actor, reason=reason)
    typer.echo(f"{environment}: rolled back to {change.bundle_id}")


@registry_app.command("active")
def registry_active(ctx: typer.Context, environment: EnvOption) -> None:
    """The active bundle of an environment and the history of its pointer."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        history = activation_history(run.engine, environment)
    if not history:
        typer.echo(f"{environment}: no active bundle")
        return
    typer.echo(f"{environment}: {history[-1].bundle_id} active")
    for change in history:
        typer.echo(
            f"  {change.activated_at} {change.action} {change.bundle_id[:12]} by {change.actor} "
            f"({change.reason})"
        )


gate_app = typer.Typer(
    help="Release gates: evaluation, vault procedure, review (GATE-001 ... GATE-003).",
    no_args_is_help=True,
)
app.add_typer(gate_app, name="gate")


@gate_app.command("evaluate")
def gate_evaluate(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
    validation: Annotated[
        str | None,
        typer.Option("--validation", help="Read this validation run instead of running one."),
    ] = None,
    exploratory: Annotated[
        bool,
        typer.Option(
            "--exploratory", help="Allow a dirty git tree for a new validation (not confirmatory)."
        ),
    ] = False,
) -> None:
    """Compile a bundle's evidence against the gates, record its R1 and R2 results and write the
    gate report (gate.md, gate.json, review.md). Exit 0 whatever the outcome; it never promotes."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        outcome = evaluate_bundle(
            run.cfg, run.engine, bundle_id, validation_run_id=validation, exploratory=exploratory
        )
    for line in outcome.summary_lines():
        typer.echo(line)
    typer.echo(f"report: {outcome.report_dir / 'gate.md'}")


@gate_app.command("vault-token")
def gate_vault_token(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
    issued_by: Annotated[str, typer.Option("--issued-by", help="Who issues it (recorded).")],
) -> None:
    """Issue a validated bundle's one vault token (GATE-002). The secret is shown once; a second
    token for the same bundle is refused."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        token = issue_vault_token(run.cfg, run.engine, bundle_id, issued_by=issued_by)
    typer.echo("vault token (shown once; it opens the vault for one run):")
    typer.echo(f"{token.token_id}.{token.secret}")


@gate_app.command("vault-evaluate")
def gate_vault_evaluate(
    ctx: typer.Context,
    bundle_id: Annotated[str, typer.Argument(help="Bundle id or a unique prefix (8+ digits).")],
    token: Annotated[str, typer.Option("--token", help="The bundle's vault token.")],
    quality_run: Annotated[
        str, typer.Option("--quality-run", help="Quality run grading the vault days.")
    ],
    last_day: Annotated[
        datetime | None,
        typer.Option(
            "--last-day",
            formats=["%Y-%m-%d"],
            help="Last trading day of the vault window (default: the last complete one).",
        ),
    ] = None,
) -> None:
    """Run the bundle's one vault evaluation and record its R3 result (confirmatory: a clean git
    tree is required). Exit 0 whatever the outcome."""
    with pipeline_run(ctx.obj) as run, cli_errors():
        outcome = run_vault_evaluation(
            run.cfg,
            run.engine,
            bundle_id,
            GateToken.parse(token),
            quality_run_id=quality_run,
            last_day=None if last_day is None else last_day.date(),
        )
    typer.echo(
        f"vault evaluation {outcome.run_id}: {outcome.days} trading days, net Sharpe "
        f"{outcome.net_sharpe:.3f}, walk-forward quantile {outcome.interval_quantile:.3f}, "
        f"{outcome.breaches} risk-limit breach(es)"
    )
    typer.echo(outcome.result.describe())
    typer.echo(f"report: {outcome.report_dir / 'vault.md'}")
