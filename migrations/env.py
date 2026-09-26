"""Alembic environment for the xq metadata database.

The connection comes from, in order: an open connection passed by `xq.tracking.db`
(``config.attributes["connection"]``), ``-x db_url=<url>``, or the configured database of profile
``$XQ_PROFILE`` (default ``dev``). Migrations use plain SQLAlchemy types only, so they never break
when application types change.
"""

import os
from typing import Any

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection, TypeDecorator

from xq.core.config import load_config
from xq.tracking.db import create_db_engine
from xq.tracking.models import Base

target_metadata = Base.metadata


def _render_item(kind: str, obj: Any, autogen_context: AutogenContext) -> str | bool:
    """Render application column types (TypeDecorators) as their plain storage type."""
    if kind == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{obj.impl!r}"
    return False


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = context.get_x_argument(as_dictionary=True).get("db_url")
    if url is None:
        url = load_config(os.environ.get("XQ_PROFILE", "dev")).database_url()
    engine = create_db_engine(url)
    with engine.begin() as new_connection:
        _run(new_connection)
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (SQL script) migrations are not supported; run against a database")
run_migrations_online()
