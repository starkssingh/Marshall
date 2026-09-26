"""Test-only gate tokens.

The library has no way to issue a vault token (GATE-002 will add the procedure), so tests insert
token rows directly to exercise verification, redemption and logging.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Engine

from xq.core.time import utc_now
from xq.datasets.vault import GateToken, secret_digest
from xq.tracking.db import session_factory
from xq.tracking.models import VaultToken

ONE_DAY = pd.Timedelta(days=1)


def seed_token(
    engine: Engine,
    token_id: str = "vt-test-0001",
    secret: str = "correct-horse-battery",
    *,
    bundle_id: str = "bundle-test",
    expires_in: pd.Timedelta = ONE_DAY,
    revoked: bool = False,
) -> GateToken:
    """Insert a vault token row and return the token a caller would present."""
    now = utc_now()
    with session_factory(engine)() as session:
        session.add(
            VaultToken(
                token_id=token_id,
                secret_sha256=secret_digest(secret),
                bundle_id=bundle_id,
                issued_by="test",
                issued_at=now,
                expires_at=now + expires_in,
                revoked=revoked,
                redeemed_at=None,
                redeemed_by_run=None,
            )
        )
        session.commit()
    return GateToken(token_id, secret)
