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
from xq.core.errors import XQError
from xq.core.ids import git_sha, new_ulid
from xq.core.logging import configure_logging, shutdown_logging
from xq.core.time import ensure_utc
from xq.data.bars import build_bar_sets
from xq.data.clean import build_clean
from xq.data.raw_store import ingest, rebuild_mirror, verify_raw_store
from xq.data.spreads import build_spread_stats
from xq.datasets.builder import build_dataset, verify_dataset
from xq.datasets.spec import load_spec
from xq.models.board import load_board_config, run_baseline_board
from xq.quality.validate import validate_source
from xq.research.eda.horizons import write_admission
from xq.research.eda.run import run_eda
from xq.tracking.conclusions import close_experiment, load_conclusion, unconcluded_experiments
from xq.tracking.db import current_revision, engine_for, head_revision, upgrade_to_head
from xq.tracking.hypotheses import register_hypothesis
from xq.tracking.registry import list_hypotheses
from xq.tracking.runs import experiment_run
from xq.tracking.trials import trial_count

EXIT_USAGE_ERROR = 2

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


@contextmanager
def pipeline_run(state: CliContext, *, source: str | None = None) -> Iterator[PipelineRun]:
    """Load config, validate `source`, bind run logging and bring the DB to the latest schema."""
    with cli_errors():
        cfg = state.config
        if source is not None:
            cfg.source(source)  # fail on an unknown source before touching anything
        run_id = new_ulid()
        sha = git_sha(cfg.paths.resolve(cfg.paths.root))
        configure_logging(cfg, run_id=run_id, git_sha=sha)
        engine = engine_for(cfg)
        try:
            upgrade_to_head(engine, cfg.paths.resolve(cfg.paths.migrations_dir))
            yield PipelineRun(cfg, engine, run_id, sha)
        finally:
            engine.dispose()
            shutdown_logging()


SourceOption = Annotated[str, typer.Option("--source", help="Configured source id.")]
StartOption = Annotated[
    datetime | None, typer.Option("--start", formats=["%Y-%m-%d"], help="First trading day.")
]
EndOption = Annotated[
    datetime | None, typer.Option("--end", formats=["%Y-%m-%d"], help="Last trading day.")
]


@app.command("ingest")
def ingest_command(
    ctx: typer.Context,
    source: SourceOption,
    path: Annotated[Path, typer.Option("--path", help="File or directory of source files.")],
) -> None:
    """Copy source files into the immutable raw store with a Parquet mirror and manifest rows.

    Files already in the raw store (same SHA-256) are skipped, so re-running is a no-op.
    """
    with pipeline_run(ctx.obj, source=source) as run:
        result = ingest(
            run.cfg, source, path, engine=run.engine, run_id=run.run_id, git_sha=run.git_sha
        )
    typer.echo(
        f"run {result.run_id}: ingested {len(result.ingested)} file(s) with {result.rows} rows; "
        f"skipped {len(result.skipped)} already in the raw store"
    )


@app.command("rebuild-mirror")
def rebuild_mirror_command(ctx: typer.Context, source: SourceOption) -> None:
    """Rewrite the Parquet mirror of a source's raw files from the stored copies."""
    with pipeline_run(ctx.obj, source=source) as run:
        count = rebuild_mirror(run.cfg, run.engine, source)
    typer.echo(f"rebuilt the mirror of {count} raw file(s)")


@app.command("clean")
def clean_command(
    ctx: typer.Context,
    source: SourceOption,
    start: StartOption = None,
    end: EndOption = None,
    force: Annotated[bool, typer.Option("--force", help="Rebuild unchanged partitions.")] = False,
) -> None:
    """Flag (and, if configured, drop) bad ticks into versioned per-trading-day partitions."""
    with pipeline_run(ctx.obj, source=source) as run:
        result = build_clean(
            run.cfg,
            run.engine,
            source,
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
    ctx: typer.Context, source: SourceOption, start: StartOption = None, end: EndOption = None
) -> None:
    """Build bid/ask/mid bars on all seven timeframes from the source's clean partitions."""
    with pipeline_run(ctx.obj, source=source) as run:
        result = build_bar_sets(
            run.cfg,
            run.engine,
            source,
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
    ctx: typer.Context, source: SourceOption, start: StartOption = None, end: EndOption = None
) -> None:
    """Compute p50/p90/p99 spreads per New York hour of week (data before the vault only)."""
    with pipeline_run(ctx.obj, source=source) as run:
        result = build_spread_stats(
            run.cfg,
            run.engine,
            source,
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
    source: SourceOption,
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
    with pipeline_run(ctx.obj, source=source) as run:
        result = validate_source(
            run.cfg,
            run.engine,
            source,
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
    help="Hypotheses, experiment runs, trial counts and conclusions (EXP-001..005).",
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


# Command groups for later phases. Each is registered now so the CLI surface is stable; the
# commands arrive in the sprint named in the help text.
_PLANNED_GROUPS = {
    "robustness": "Robustness stress tests (Sprint 9: ROB-001..008).",
    "registry": "Model registry and strategy bundles (Sprint 13: MREG-001..005).",
    "gate": "Evidence gates and vault evaluation (Sprint 13: GATE-001..003).",
}

for _name, _help in _PLANNED_GROUPS.items():
    app.add_typer(typer.Typer(help=_help, no_args_is_help=True), name=_name)
