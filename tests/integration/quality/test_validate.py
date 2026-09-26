"""DQ-006: `xq validate` grades every check per trading day, persists results, writes a report."""

import json
from datetime import date
from pathlib import Path

import pytest
import structlog
from sqlalchemy import func, select
from typer.testing import CliRunner

from helpers.pipeline import REPO, config, run_pipeline
from xq.cli.main import app
from xq.core.errors import VaultAccessError
from xq.quality.registry import Status
from xq.quality.validate import NoQualityDataError, validate_source
from xq.tracking.db import session_factory
from xq.tracking.models import QualityResultRecord, QualityRunRecord

WEEK = [date(2024, 3, d) for d in (11, 12, 13, 14, 15)]


def test_clean_week_passes_and_is_persisted(tmp_path: Path, clean_week_dir: Path) -> None:
    cfg = config(tmp_path)
    engine = run_pipeline(cfg, clean_week_dir)
    result = validate_source(
        cfg, engine, "mt5_primary", run_id="01QRUN000000000000000000AA", git_sha="test"
    )
    assert result.days == WEEK
    assert result.results
    assert {r.status for r in result.results} == {Status.PASS}
    checks = result.summary["checks"]
    assert checks["tick.rate_anomalies"]["days_evaluated"] == 0  # one week is too little history
    assert checks["cal.holiday_behaviour"]["days_evaluated"] == 0  # no holidays that week
    assert checks["tick.spread_outliers"]["days_evaluated"] == 5

    report_dir = result.report_path.parent
    assert {p.name for p in report_dir.iterdir()} == {
        "report.md",
        "summary.json",
        "missing_minutes.png",
        "spreads.png",
    }
    text = result.report_path.read_text()
    assert "Results: " in text
    assert "`cal.gap_location`" in text
    assert json.loads((report_dir / "summary.json").read_text())["totals"]["fail"] == 0

    with session_factory(engine)() as session:
        run = session.get(QualityRunRecord, "01QRUN000000000000000000AA")
        stored = session.scalar(select(func.count()).select_from(QualityResultRecord))
    assert run is not None
    assert run.includes_vault is False
    assert run.report_path == str(result.report_path)
    assert run.summary_json["totals"] == result.summary["totals"]
    assert stored == len(result.results)
    engine.dispose()


def test_misdeclared_clock_fails_calendar_checks(tmp_path: Path, clean_week_dir: Path) -> None:
    # The file is in NY+7 server time; declaring EET puts every tick an hour late in March.
    cfg = config(tmp_path, **{"sources.mt5_primary.clock": "tz:Europe/Athens"})
    engine = run_pipeline(cfg, clean_week_dir, spreads=False)
    result = validate_source(
        cfg, engine, "mt5_primary", run_id="01QRUN000000000000000000AB", git_sha="test"
    )
    by_check = {(r.check_id, r.trading_day): r for r in result.results}
    for day in WEEK[1:]:
        assert by_check[("cal.gap_location", day)].status is Status.FAIL
        assert by_check[("cal.closed_market_ticks", day)].status is Status.FAIL
    assert set(result.summary["days_with_failures"]) >= {d.isoformat() for d in WEEK[1:]}
    engine.dispose()


def test_dropped_duplicates_are_still_reported(tmp_path: Path, clean_week_dir: Path) -> None:
    overlap = tmp_path / "overlap"
    overlap.mkdir()
    lines = next(clean_week_dir.iterdir()).read_text().splitlines(keepends=True)
    (overlap / "XAUUSD_partial.csv").write_text("".join(lines[:3001]))
    cfg = config(tmp_path, **{"cleaning.drop": ["DUP_EXACT"]})
    engine = run_pipeline(cfg, clean_week_dir, overlap)
    result = validate_source(
        cfg, engine, "mt5_primary", run_id="01QRUN000000000000000000AC", git_sha="test"
    )
    (monday,) = [
        r
        for r in result.results
        if r.check_id == "tick.duplicates_exact" and r.trading_day == WEEK[0]
    ]
    assert monday.details["dropped"] == 3000
    assert monday.metric == pytest.approx(3000 / monday.details["ticks"])
    assert monday.status is Status.WARN  # ~19% of Monday's ticks: above 1%, under 20%
    engine.dispose()


def test_vault_days_need_explicit_inclusion(tmp_path: Path, clean_week_dir: Path) -> None:
    cfg = config(tmp_path, **{"vault.start": "2024-03-13T21:00:00Z"})  # start of 14 March
    engine = run_pipeline(cfg, clean_week_dir, spreads=False)
    default = validate_source(
        cfg, engine, "mt5_primary", run_id="01QRUN000000000000000000AD", git_sha="test"
    )
    assert default.days == WEEK[:3]
    with pytest.raises(VaultAccessError, match="--i-understand-vault-access"):
        validate_source(
            cfg,
            engine,
            "mt5_primary",
            run_id="01QRUN000000000000000000AH",
            git_sha="test",
            include_vault=True,
        )
    with structlog.testing.capture_logs() as events:
        everything = validate_source(
            cfg,
            engine,
            "mt5_primary",
            run_id="01QRUN000000000000000000AE",
            git_sha="test",
            include_vault=True,
            vault_access_confirmed=True,
        )
    assert everything.days == WEEK
    (access,) = [e for e in events if e["event"] == "vault_validation_access"]
    assert access["log_level"] == "warning"
    assert access["run_id"] == "01QRUN000000000000000000AE"
    assert access["source_id"] == "mt5_primary"
    assert access["vault_days"] == ["2024-03-14", "2024-03-15"]
    with session_factory(engine)() as session:
        flagged = session.get(QualityRunRecord, "01QRUN000000000000000000AE")
        refused = session.get(QualityRunRecord, "01QRUN000000000000000000AH")
    assert flagged is not None
    assert flagged.includes_vault is True
    assert refused is None
    engine.dispose()


def test_cli_include_vault_needs_confirmation(tmp_path: Path, clean_week_dir: Path) -> None:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--set",
        f"paths.root={tmp_path}",
        "--set",
        f"paths.migrations_dir={REPO / 'migrations'}",
        "--set",
        "logging.console=false",
        "--set",
        "vault.start=2024-03-13T21:00:00Z",
    ]
    runner = CliRunner()
    for step in (["ingest", "--path", str(clean_week_dir)], ["clean"], ["build-bars"]):
        result = runner.invoke(app, [*common, step[0], "--source", "mt5_primary", *step[1:]])
        assert result.exit_code == 0, result.output
    validate = [*common, "validate", "--source", "mt5_primary", "--include-vault"]
    refused = runner.invoke(app, validate)
    assert refused.exit_code == 2
    assert "--i-understand-vault-access" in refused.output
    confirmed = runner.invoke(app, [*validate, "--i-understand-vault-access"])
    assert confirmed.exit_code == 0, confirmed.output
    assert "5 trading day(s)" in confirmed.stdout
    assert "includes vault days" in confirmed.stdout
    log = (tmp_path / "logs" / "xq.jsonl").read_text().splitlines()
    assert any(json.loads(line)["event"] == "vault_validation_access" for line in log)


def test_missing_inputs_are_explained(tmp_path: Path, clean_week_dir: Path) -> None:
    cfg = config(tmp_path)
    engine = run_pipeline(cfg, clean_week_dir, bars=False, spreads=False)
    with pytest.raises(NoQualityDataError, match="xq build-bars"):
        validate_source(
            cfg, engine, "mt5_primary", run_id="01QRUN000000000000000000AF", git_sha="test"
        )
    with pytest.raises(NoQualityDataError, match="xq clean"):
        validate_source(
            cfg,
            engine,
            "mt5_primary",
            run_id="01QRUN000000000000000000AG",
            git_sha="test",
            start=date(2020, 1, 1),
            end=date(2020, 1, 31),
        )
    engine.dispose()


def test_cli_validate(tmp_path: Path, clean_week_dir: Path) -> None:
    common = [
        "--config-dir",
        str(REPO / "config"),
        "--profile",
        "research",
        "--set",
        f"paths.root={tmp_path}",
        "--set",
        f"paths.migrations_dir={REPO / 'migrations'}",
        "--set",
        "logging.console=false",
    ]
    runner = CliRunner()
    steps = [
        ["ingest", "--path", str(clean_week_dir)],
        ["clean"],
        ["build-bars"],
        ["spread-stats"],
        ["validate"],
    ]
    for step in steps:
        result = runner.invoke(app, [*common, step[0], "--source", "mt5_primary", *step[1:]])
        assert result.exit_code == 0, result.output
    assert "5 trading day(s)" in result.stdout
    assert " 0 warn, 0 fail" in result.stdout
    assert "report: " in result.stdout
