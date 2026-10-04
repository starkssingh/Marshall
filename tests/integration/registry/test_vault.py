"""GATE-002 end to end on synthetic ticks that span a test vault start: only a validated bundle
gets a vault token, and only one; a token of another bundle and a quality run that does not grade
the vault days are refused before the vault is read; the one evaluation records R3 and logs every
read; a second vault access for the same bundle is refused. The R1 and R2 results that bring the
bundles to ``validated`` are recorded directly from synthetic checks: this module tests the vault
procedure, and the gate evaluator is tested in test_gate_evaluate.py. Synthetic data only."""

from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, func, select

from helpers.datasets import QUALITY_RUN
from helpers.gate_board import VAULT_QUALITY_RUN, build_world, register, xq
from xq.core.config import AppConfig
from xq.registry.bundles import get_bundle, performance_history
from xq.registry.gates import EvidenceTier, latest_gate_result, promote, record_gate_result
from xq.registry.models import Status, SubjectKind
from xq.registry.vault import VAULT_KIND
from xq.tracking import registry
from xq.tracking.db import session_factory
from xq.tracking.models import VaultAccess, VaultToken

LAST_DAY = "2024-03-22"  # the last complete trading day of the synthetic ticks


@pytest.fixture(scope="module")
def world(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, AppConfig, Engine, str]]:
    root = tmp_path_factory.mktemp("vault")
    cfg, engine, _, run_id = build_world(root, tmp_path_factory.mktemp("vault_ticks"))
    yield root, cfg, engine, run_id
    engine.dispose()


def validated(cfg: AppConfig, engine: Engine, bundle_id: str) -> None:
    """Bring a bundle to validated with synthetic passing R1 and R2 results (module docstring)."""
    gates = cfg.gates_config()
    for gate, status in (("R1", Status.CANDIDATE), ("R2", Status.VALIDATED)):
        record_gate_result(
            engine,
            gates,
            subject_kind=SubjectKind.BUNDLE,
            subject_id=bundle_id,
            gate=gate,
            checks=[
                c.check(c.threshold + (0.001 if c.op in (">", ">=") else -0.001))
                for c in gates.criteria()
                if c.gate == gate
            ],
            not_evaluated={},
            not_applicable={},
            evaluator="test: synthetic results to reach the vault step",
            evidence_paths=[],
            run_id=None,
        )
        promote(engine, SubjectKind.BUNDLE, bundle_id, status, actor="test", reason="synthetic")


def token_of(output: str) -> str:
    return output.strip().splitlines()[-1]


def vault_reads(engine: Engine) -> int:
    with session_factory(engine)() as session:
        return int(session.scalar(select(func.count()).select_from(VaultAccess)) or 0)


def test_the_vault_is_opened_once_per_validated_bundle(
    world: tuple[Path, AppConfig, Engine, str],
) -> None:
    root, cfg, engine, run_id = world
    first = register(root, run_id, "tsmom_8@4h")
    second = register(root, run_id, "buy_and_hold@1h")
    # a bundle's signal timeframe is its rule's, not the board's first (C-15)
    assert get_bundle(engine, first).content.strategy.signal_timeframe == "4h"
    assert get_bundle(engine, second).content.strategy.signal_timeframe == "1h"
    code, output = xq(root, "gate", "vault-token", first[:12], "--issued-by", "owner")
    assert code == 2
    assert "is draft: only a validated bundle" in output
    for bundle_id in (first, second):
        validated(cfg, engine, bundle_id)
    code, output = xq(root, "gate", "vault-token", first[:12], "--issued-by", "owner")
    assert code == 0, output
    token = token_of(output)
    token_id, secret = token.split(".")
    with session_factory(engine)() as session:
        record = session.get(VaultToken, token_id)
        assert record is not None
        assert record.bundle_id == first
        assert record.issued_by == "owner"
        assert secret not in record.secret_sha256  # only the hash is stored
        assert record.expires_at - record.issued_at == pd.Timedelta(hours=24)
    code, output = xq(root, "gate", "vault-token", first[:12], "--issued-by", "owner")
    assert code == 2
    assert "a second vault access for the same bundle is refused" in output
    code, output = xq(root, "gate", "vault-token", second[:12], "--issued-by", "owner")
    assert code == 0, output
    other = token_of(output)

    # refused before any vault read: another bundle's token, a quality run missing the vault days
    code, output = xq(
        root, "gate", "vault-evaluate", second[:12], "--token", token,
        "--quality-run", VAULT_QUALITY_RUN, "--last-day", LAST_DAY,
    )  # fmt: skip
    assert code == 2
    assert "does not belong to bundle" in output
    code, output = xq(
        root, "gate", "vault-evaluate", second[:12], "--token", other,
        "--quality-run", QUALITY_RUN, "--last-day", LAST_DAY,
    )  # fmt: skip
    assert code == 2
    assert "does not grade 2024-03-19" in output
    assert vault_reads(engine) == 0

    # the one evaluation
    code, output = xq(
        root, "gate", "vault-evaluate", first[:12], "--token", token,
        "--quality-run", VAULT_QUALITY_RUN, "--last-day", LAST_DAY,
    )  # fmt: skip
    assert code == 0, output
    (run,) = [
        r for r in registry.list_runs(engine) if r.kind == VAULT_KIND and r.status == "finished"
    ]
    assert run.confirmatory
    assert run.config["run"]["bundle"] == first
    with session_factory(engine)() as session:
        reads = session.scalars(select(VaultAccess).where(VaultAccess.run_id == run.run_id)).all()
        record = session.get(VaultToken, token_id)
        assert record is not None
        assert record.redeemed_by_run == run.run_id
    assert len(reads) >= 2  # bars and ticks: every read logged
    r3 = latest_gate_result(engine, SubjectKind.BUNDLE, first, "R3")
    assert r3 is not None
    assert r3.run_id == run.run_id
    assert sorted(r3.values["checks"]) == [
        "net_sharpe_min",
        "risk_limit_breaches_max",
        "vault_access_logged",
        "walk_forward_interval.high",
        "walk_forward_interval.low",
    ]
    assert r3.values["checks"]["vault_access_logged"]["value"] == 1.0
    assert r3.evidence_tier is EvidenceTier.SCREENING  # never promotes to paper (ADR 0062)
    assert r3.describe() in output
    vault = performance_history(engine, first, "vault")
    assert [str(d) for d in vault["trading_day"]] == [
        "2024-03-19",
        "2024-03-20",
        "2024-03-21",
        "2024-03-22",
    ]
    report = Path(output.strip().splitlines()[-1].removeprefix("report: "))
    assert report.read_text().startswith(f"# Vault evaluation of bundle `{first[:12]}`")

    # a second access for the same bundle: the token is spent and no new one is issued
    code, output = xq(
        root, "gate", "vault-evaluate", first[:12], "--token", token,
        "--quality-run", VAULT_QUALITY_RUN, "--last-day", LAST_DAY,
    )  # fmt: skip
    assert code == 2
    assert "was already used by run" in output
    code, output = xq(root, "gate", "vault-token", first[:12], "--issued-by", "owner")
    assert code == 2
    assert "second vault access" in output
    # promotion follows R3: only a passing R3 moves the bundle to vault_passed
    code, output = xq(
        root, "registry", "promote", first[:12], "--to", "vault_passed", "--actor", "owner",
        "--reason", "R3",
    )  # fmt: skip
    assert (code == 0) is r3.passed, output
    expected = Status.VAULT_PASSED if r3.passed else Status.VALIDATED
    assert get_bundle(engine, first).status is expected
