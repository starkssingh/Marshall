"""Metadata database engine, sessions and migrations (DATA-005)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from xq.core.config import AppConfig
from xq.core.errors import ConfigError


def create_db_engine(url: str) -> Engine:
    """Create an engine; SQLite databases get their directory created and foreign keys enforced."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if parsed.get_backend_name() == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def engine_for(cfg: AppConfig) -> Engine:
    """Create the engine for the configured metadata database."""
    return create_db_engine(cfg.database_url())


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory; objects stay usable after commit."""
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(migrations_dir: Path) -> AlembicConfig:
    """Alembic configuration pointing at `migrations_dir`, without an ini file."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(migrations_dir))
    return config


def upgrade_to_head(engine: Engine, migrations_dir: Path) -> None:
    """Apply all pending migrations. Safe to call repeatedly."""
    _run(engine, migrations_dir, command.upgrade, "head")


def downgrade_to(engine: Engine, migrations_dir: Path, revision: str) -> None:
    """Revert migrations down to `revision` ("base" removes everything)."""
    _run(engine, migrations_dir, command.downgrade, revision)


def current_revision(engine: Engine) -> str | None:
    """The revision the database is at, or None if it has never been migrated."""
    with engine.connect() as connection:
        revision: str | None = MigrationContext.configure(connection).get_current_revision()
        return revision


def head_revision(migrations_dir: Path) -> str | None:
    """The newest revision available in `migrations_dir`."""
    head: str | None = ScriptDirectory.from_config(
        alembic_config(migrations_dir)
    ).get_current_head()
    return head


def _run(engine: Engine, migrations_dir: Path, action: Any, revision: str) -> None:
    if not (migrations_dir / "env.py").is_file():
        raise ConfigError(f"migrations directory not found: {migrations_dir}")
    config = alembic_config(migrations_dir)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        action(config, revision)


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
