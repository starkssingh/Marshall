"""The `xq` command-line entry point (ARCH-006).

Global options select the configuration (``--profile``, ``--set``, ``--config-dir``); commands load
it lazily so ``xq --help`` and ``xq --version`` work without a config directory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml

import xq
from xq.core.config import AppConfig, config_as_dict, config_hash, load_config, parse_override
from xq.core.errors import XQError
from xq.tracking.db import current_revision, engine_for, head_revision, upgrade_to_head

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
    "dataset": "Build and inspect versioned datasets (Sprint 3: DS-001..007).",
    "exp": "Hypotheses, experiment runs and reproduction (Sprint 3: EXP-001..006).",
    "baselines": "Walk-forward baseline board (Sprint 4: BASE-001..006).",
    "research": "Exploratory, statistical and volatility research (Sprints 5-8).",
    "robustness": "Robustness stress tests (Sprint 9: ROB-001..008).",
    "registry": "Model registry and strategy bundles (Sprint 13: MREG-001..005).",
    "gate": "Evidence gates and vault evaluation (Sprint 13: GATE-001..003).",
}

for _name, _help in _PLANNED_GROUPS.items():
    app.add_typer(typer.Typer(help=_help, no_args_is_help=True), name=_name)
