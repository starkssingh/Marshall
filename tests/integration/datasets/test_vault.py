"""DS-004: vault enforcement with one-time gate tokens."""

from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
import structlog
from sqlalchemy import Engine, select

from helpers.pipeline import REPO, config
from helpers.vault import seed_token
from xq.core.config import AppConfig
from xq.core.errors import VaultAccessError
from xq.datasets.vault import GateToken, check_window, vault_start
from xq.tracking.db import create_db_engine, session_factory, upgrade_to_head
from xq.tracking.models import VaultAccess, VaultToken

RUN_A = "01RUNA0000000000000000000A"
RUN_B = "01RUNB0000000000000000000B"


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return config(tmp_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    engine = create_db_engine(cfg.database_url())
    upgrade_to_head(engine, REPO / "migrations")
    yield engine
    engine.dispose()


def into_vault(cfg: AppConfig) -> tuple[pd.Timestamp, pd.Timestamp]:
    vault = vault_start(cfg)
    return vault - pd.Timedelta(days=1), vault + pd.Timedelta(hours=1)


def test_pre_vault_windows_need_no_token(cfg: AppConfig) -> None:
    vault = vault_start(cfg)
    assert check_window(cfg, vault - pd.Timedelta(days=3), vault) is False


def test_vault_windows_without_a_token_are_refused(cfg: AppConfig) -> None:
    with pytest.raises(VaultAccessError, match=r"vault starts at .* gate token"):
        check_window(cfg, *into_vault(cfg))


def test_a_token_needs_the_database_and_a_run(cfg: AppConfig, engine: Engine) -> None:
    token = seed_token(engine)
    with pytest.raises(VaultAccessError, match="metadata database"):
        check_window(cfg, *into_vault(cfg), token=token, run_id=RUN_A)
    with pytest.raises(VaultAccessError, match="one run"):
        check_window(cfg, *into_vault(cfg), token=token, engine=engine)


@pytest.mark.parametrize(
    ("kwargs", "presented", "message"),
    [
        ({}, GateToken("vt-test-0001", "wrong-secret"), "invalid gate token"),
        ({}, GateToken("vt-unknown", "correct-horse-battery"), "invalid gate token"),
        ({"revoked": True}, None, "revoked"),
        ({"expires_in": pd.Timedelta(seconds=-1)}, None, "expired"),
    ],
)
def test_bad_tokens_are_refused(
    cfg: AppConfig,
    engine: Engine,
    kwargs: dict[str, object],
    presented: GateToken | None,
    message: str,
) -> None:
    token = seed_token(engine, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(VaultAccessError, match=message):
        check_window(cfg, *into_vault(cfg), token=presented or token, engine=engine, run_id=RUN_A)
    with session_factory(engine)() as session:
        assert session.scalars(select(VaultAccess)).all() == []


def test_a_token_opens_the_vault_for_one_run_and_every_use_is_logged(
    cfg: AppConfig, engine: Engine
) -> None:
    token = seed_token(engine)
    start, end = into_vault(cfg)
    with structlog.testing.capture_logs() as events:
        assert check_window(cfg, start, end, token=token, engine=engine, run_id=RUN_A, purpose="p1")
        assert check_window(cfg, start, end, token=token, engine=engine, run_id=RUN_A, purpose="p2")
    granted = [e for e in events if e["event"] == "vault_access_granted"]
    assert [e["purpose"] for e in granted] == ["p1", "p2"]
    assert all(e["log_level"] == "warning" and e["bundle_id"] == "bundle-test" for e in granted)
    assert all("correct-horse-battery" not in str(e) for e in events)

    with pytest.raises(VaultAccessError, match=f"already used by run {RUN_A}"):
        check_window(cfg, start, end, token=token, engine=engine, run_id=RUN_B)

    with session_factory(engine)() as session:
        record = session.get(VaultToken, token.token_id)
        accesses = session.scalars(select(VaultAccess).order_by(VaultAccess.access_id)).all()
    assert record is not None
    assert record.redeemed_by_run == RUN_A
    assert record.redeemed_at is not None
    assert [(a.run_id, a.purpose) for a in accesses] == [(RUN_A, "p1"), (RUN_A, "p2")]
    assert accesses[0].window_start_utc == start
    assert accesses[0].window_end_utc == end


def test_token_text_form_and_secrecy() -> None:
    token = GateToken.parse("vt-abc.s3cret")
    assert token == GateToken("vt-abc", "s3cret")
    assert "s3cret" not in repr(token)
    assert "s3cret" not in str(token)
    for text in ("vt-abc", ".s3cret", "vt-abc."):
        with pytest.raises(VaultAccessError, match="malformed"):
            GateToken.parse(text)
