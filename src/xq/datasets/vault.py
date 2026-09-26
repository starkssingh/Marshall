"""Vault enforcement with one-time gate tokens (DS-004).

The vault is every instant at or after ``vault.start``. Loaders call `check_window` before they
read: a window that ends at or before ``vault.start`` passes; a window that reaches past it raises
`VaultAccessError` unless the caller presents a valid `GateToken`.

A token belongs to one strategy bundle and unlocks the vault for **one run**: the first run that
presents it redeems it, and any other run is refused. Every granted access is logged as a
``vault_access_granted`` warning and stored in ``vault_access_log``.

Tokens are issued only by the release-gate procedure (GATE-002, Sprint 13). This module verifies
and redeems them; it deliberately has no way to create one, so until GATE-002 exists the vault
cannot be opened through the library.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import Engine

from xq.core.config import AppConfig
from xq.core.errors import VaultAccessError
from xq.core.logging import get_logger
from xq.core.time import TimestampLike, ensure_utc, utc_now
from xq.tracking.db import session_factory
from xq.tracking.models import VaultAccess, VaultToken

log = get_logger(__name__)


@dataclass(frozen=True)
class GateToken:
    """A vault gate token: its public id and its secret (never logged or stored in clear)."""

    token_id: str
    secret: str = field(repr=False)

    @classmethod
    def parse(cls, text: str) -> GateToken:
        """Parse ``<token_id>.<secret>``, the form GATE-002 hands out."""
        token_id, sep, secret = text.strip().partition(".")
        if not sep or not token_id or not secret:
            raise VaultAccessError("malformed gate token; expected <token_id>.<secret>")
        return cls(token_id, secret)

    def __str__(self) -> str:
        return f"{self.token_id}.<secret>"


def vault_start(cfg: AppConfig) -> pd.Timestamp:
    """The first instant of the vault (UTC)."""
    return pd.Timestamp(cfg.vault.start)


def secret_digest(secret: str) -> str:
    """SHA-256 hex digest of a token secret (what ``vault_tokens`` stores)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def check_window(
    cfg: AppConfig,
    start: TimestampLike,
    end: TimestampLike,
    *,
    token: GateToken | None = None,
    engine: Engine | None = None,
    run_id: str | None = None,
    purpose: str = "",
) -> bool:
    """Allow a read of ``[start, end)`` or raise.

    Returns:
        False if the window ends at or before ``vault.start`` (no vault data involved); True if it
        reaches into the vault and `token` was accepted (the access is logged and recorded).

    Raises:
        VaultAccessError: the window reaches into the vault and no valid token was given, the
            token is unknown, revoked, expired or already redeemed by another run, or the token
            cannot be verified (no database or no run id).
    """
    window_start, window_end = ensure_utc(start), ensure_utc(end)
    vault = vault_start(cfg)
    if window_end <= vault:
        return False
    if token is None:
        raise VaultAccessError(
            f"requested data up to {window_end}, but the vault starts at {vault}; research data "
            "must end at or before vault.start (a vault read needs a gate token from GATE-002)"
        )
    if engine is None or not run_id:
        raise VaultAccessError(
            "a gate token is verified against the metadata database and bound to one run; "
            "pass the engine and the run id"
        )
    _redeem(engine, token, run_id=run_id, purpose=purpose, start=window_start, end=window_end)
    return True


def _redeem(
    engine: Engine,
    token: GateToken,
    *,
    run_id: str,
    purpose: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    now = utc_now()
    with session_factory(engine)() as session:
        record = session.get(VaultToken, token.token_id)
        if record is None or not hmac.compare_digest(
            record.secret_sha256, secret_digest(token.secret)
        ):
            raise VaultAccessError(f"invalid gate token {token}")
        if record.revoked:
            raise VaultAccessError(f"gate token {token} has been revoked")
        if now >= record.expires_at:
            raise VaultAccessError(f"gate token {token} expired at {record.expires_at}")
        if record.redeemed_by_run is not None and record.redeemed_by_run != run_id:
            raise VaultAccessError(
                f"gate token {token} was already used by run {record.redeemed_by_run}; "
                "each token opens the vault for one run only"
            )
        if record.redeemed_by_run is None:
            record.redeemed_at = now
            record.redeemed_by_run = run_id
        session.add(
            VaultAccess(
                token_id=record.token_id,
                run_id=run_id,
                purpose=purpose,
                window_start_utc=start,
                window_end_utc=end,
                accessed_at=now,
            )
        )
        session.commit()
        bundle_id = record.bundle_id
    log.warning(
        "vault_access_granted",
        token_id=token.token_id,
        bundle_id=bundle_id,
        run_id=run_id,
        purpose=purpose,
        window_start=str(start),
        window_end=str(end),
    )
