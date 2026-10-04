"""Structured JSON logging with run context (ARCH-004).

`configure_logging` routes structlog and standard-library loggers (SQLAlchemy, Alembic, ...) through
the same processors, so every line — ours or a library's — is a JSON object carrying ``run_id``,
``git_sha`` and ``config_hash``. Logs go to stderr (keeping stdout clean for command output) and,
if configured, to a JSON-lines file. Third-party libraries named in ``logging.third_party``
(matplotlib, PIL, ...) log at ``logging.third_party_level`` (WARNING), not at the root level
(ADR 0062).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.typing import Processor

from xq.core.config import AppConfig, config_hash

_HANDLER_MARKER = "_xq_handler"


def configure_logging(cfg: AppConfig, *, run_id: str, git_sha: str) -> None:
    """Configure logging for one process run and bind the run context to every line.

    Calling it again replaces the handlers installed by the previous call.

    Args:
        cfg: Resolved configuration; `cfg.logging` controls level, console and file output.
        run_id: Identifier of this run (a ULID from `xq.core.ids.new_ulid`).
        git_sha: Commit the code was run from (`xq.core.ids.git_sha`).
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    shutdown_logging()
    root = logging.getLogger()
    root.setLevel(cfg.logging.level)
    for name in cfg.logging.third_party:
        logging.getLogger(name).setLevel(cfg.logging.third_party_level)

    if cfg.logging.console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(_formatter(shared, json=cfg.logging.console_format == "json"))
        _install(root, console)

    if cfg.logging.file is not None:
        path = cfg.paths.resolve(cfg.logging.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(_formatter(shared, json=True))
        _install(root, file_handler)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        run_id=run_id, git_sha=git_sha, config_hash=config_hash(cfg)
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger; bind extra context with ``.bind(key=value)``."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def shutdown_logging() -> None:
    """Remove and close the handlers installed by `configure_logging`."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()


def _formatter(shared: list[Processor], *, json: bool) -> logging.Formatter:
    renderer: list[Processor]
    if json:
        renderer = [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
    else:
        renderer = [structlog.dev.ConsoleRenderer(colors=False)]
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
    )


def _install(root: logging.Logger, handler: logging.Handler) -> None:
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)


def bound_context() -> dict[str, Any]:
    """Return the context currently bound to every log line (useful for run metadata)."""
    return dict(structlog.contextvars.get_contextvars())
