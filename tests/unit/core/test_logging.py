"""ARCH-004: structured JSON logging with run context."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from xq.core.config import AppConfig, config_hash, load_config
from xq.core.logging import bound_context, configure_logging, get_logger, shutdown_logging


def _config(tmp_path: Path, **logging_overrides: Any) -> AppConfig:
    settings = {"file": "logs/xq.jsonl", "console": False, **logging_overrides}
    return AppConfig.model_validate(
        {
            "profile": "test",
            "paths": {"root": str(tmp_path)},
            "logging": settings,
            "vault": {"start": "2025-09-25T21:00:00Z"},
        }
    )


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    shutdown_logging()
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


def test_every_line_carries_run_context(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    configure_logging(cfg, run_id="01RUN", git_sha="abc123")

    get_logger("xq.test").info("ingest_started", files=3)
    logging.getLogger("sqlalchemy.engine").warning("library message %s", "x")

    lines = _lines(tmp_path / "logs" / "xq.jsonl")
    assert [line["event"] for line in lines] == ["ingest_started", "library message x"]
    for line in lines:
        assert line["run_id"] == "01RUN"
        assert line["git_sha"] == "abc123"
        assert line["config_hash"] == config_hash(cfg)
        assert line["timestamp"].endswith("Z")
    assert lines[0]["files"] == 3
    assert lines[0]["logger"] == "xq.test"
    assert lines[0]["level"] == "info"


def test_level_filters_lines(tmp_path: Path) -> None:
    configure_logging(_config(tmp_path, level="WARNING"), run_id="r", git_sha="g")
    log = get_logger("xq.test")
    log.info("dropped")
    log.warning("kept")
    assert [line["event"] for line in _lines(tmp_path / "logs" / "xq.jsonl")] == ["kept"]


def test_console_output_is_json_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(_config(tmp_path, console=True, file=None), run_id="r", git_sha="g")
    get_logger("xq.test").info("hello", answer=42)
    captured = capsys.readouterr()
    assert captured.out == ""
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert record["event"] == "hello"
    assert record["answer"] == 42
    assert record["run_id"] == "r"


def test_exceptions_are_structured(tmp_path: Path) -> None:
    configure_logging(_config(tmp_path), run_id="r", git_sha="g")
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("xq.test").exception("failed")
    (record,) = _lines(tmp_path / "logs" / "xq.jsonl")
    assert record["exception"][0]["exc_type"] == "ValueError"


def test_reconfiguring_does_not_duplicate_handlers(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    configure_logging(cfg, run_id="first", git_sha="g")
    configure_logging(cfg, run_id="second", git_sha="g")
    get_logger("xq.test").info("once")
    lines = _lines(tmp_path / "logs" / "xq.jsonl")
    assert len(lines) == 1
    assert lines[0]["run_id"] == "second"
    assert bound_context()["run_id"] == "second"


def test_third_party_loggers_are_held_at_warning(tmp_path: Path) -> None:
    """ADR 0062: at DEBUG, matplotlib's findfont lines and PIL's debug lines are dropped, their
    warnings kept, and the project's own debug lines kept."""
    configure_logging(_config(tmp_path, level="DEBUG"), run_id="r", git_sha="g")
    logging.getLogger("matplotlib.font_manager").debug("findfont: Matching sans-serif")
    logging.getLogger("PIL.PngImagePlugin").debug("STREAM b'IHDR'")
    logging.getLogger("matplotlib.font_manager").warning("findfont: Font family not found")
    get_logger("xq.test").debug("ours")
    events = [line["event"] for line in _lines(tmp_path / "logs" / "xq.jsonl")]
    assert events == ["findfont: Font family not found", "ours"]
    assert logging.getLogger("matplotlib").level == logging.WARNING


def test_the_repository_config_quiets_matplotlib_and_pil() -> None:
    cfg = load_config("dev", config_dir=Path(__file__).resolve().parents[3] / "config")
    assert cfg.logging.level == "DEBUG"
    assert {"matplotlib", "PIL"} <= set(cfg.logging.third_party)
    assert cfg.logging.third_party_level == "WARNING"
