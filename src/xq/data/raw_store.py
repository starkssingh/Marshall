"""Immutable raw store, Parquet mirror and manifest (DATA-004).

Ingesting a source file:

1. hash it (SHA-256); if the hash is already in ``raw_files``, skip it — re-ingest is a no-op;
2. copy it into ``data/raw/<source>/<instrument>/<yyyy>/<mm>/<raw_file_id>__<original name>``,
   verify the copy's hash and make it read-only; everything downstream reads this copy;
3. write a faithful Parquet mirror of it — every row, with ``raw_file_id`` and ``row_num`` — to
   ``data/raw_parquet/<source>/<instrument>/year=YYYY/month=MM/day=DD/part-<raw_file_id>.parquet``,
   split by the UTC day of each row;
4. insert its ``raw_files`` manifest row. A file counts as ingested only once that row exists, so
   an interrupted run leaves nothing half-recorded; a rerun adopts an identical stored copy.

``raw_file_id`` is the first 16 hex digits of the file's SHA-256, so it is the same whenever the
same file is ingested, and a rebuilt store reproduces the same ids.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from xq.core.config import AppConfig, config_hash
from xq.core.errors import RawStoreIntegrityError
from xq.core.logging import get_logger
from xq.core.time import from_ns, utc_now
from xq.data.adapters import RawFileRef, SourceAdapter, build_adapter
from xq.data.provenance import register_source
from xq.tracking.db import session_factory
from xq.tracking.models import IngestRun, RawFile

RAW_DIR = "raw"
MIRROR_DIR = "raw_parquet"
INCOMING_DIR = ".incoming"
READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_CHUNK = 1 << 20
_DAY_NS = 86_400 * 1_000_000_000

log = get_logger(__name__)


@dataclass(frozen=True)
class IngestResult:
    """What one `ingest` call did."""

    run_id: str
    ingested: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rows: int = 0


@dataclass(frozen=True)
class IntegrityProblem:
    """A raw-store file that no longer matches its manifest row."""

    raw_file_id: str
    path: Path
    problem: str


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def raw_file_id_for(sha256: str) -> str:
    """Content-derived id of a raw file."""
    return sha256[:16]


def ingest(
    cfg: AppConfig, source_id: str, path: Path, *, engine: Engine, run_id: str, git_sha: str
) -> IngestResult:
    """Ingest every file of `source_id` found at `path` into the raw store.

    The metadata database must already be at the current migration (``xq db upgrade``).
    """
    adapter = build_adapter(cfg, source_id)
    source = cfg.source(source_id)
    instrument = cfg.instrument(source.instrument, source.venue)
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    refs = adapter.discover(path)
    started = utc_now()
    ingested: list[str] = []
    skipped: list[str] = []
    rows = 0

    with session_factory(engine)() as session:
        register_source(session, source_id, source, instrument)
        run = IngestRun(
            run_id=run_id,
            started_at=started,
            finished_at=None,
            status="running",
            git_sha=git_sha,
            config_hash=config_hash(cfg),
            params_json={"source_id": source_id, "path": str(path), "files_found": len(refs)},
        )
        session.add(run)
        session.commit()
        log.info("ingest_started", source_id=source_id, path=str(path), files=len(refs))
        try:
            for ref in refs:
                digest = sha256_file(ref.path)
                existing = session.scalar(select(RawFile).where(RawFile.sha256 == digest))
                if existing is not None:
                    skipped.append(existing.raw_file_id)
                    _log_skip(ref, existing, source_id)
                    continue
                record = _ingest_file(
                    session, adapter, ref, digest, data_dir, source_id, source.instrument, run_id
                )
                session.commit()
                ingested.append(record.raw_file_id)
                rows += record.row_count
                log.info(
                    "raw_file_ingested",
                    file=str(ref.path),
                    raw_file_id=record.raw_file_id,
                    rows=record.row_count,
                )
        except Exception:
            session.rollback()
            _finish(session, run, "failed", ingested, skipped, rows)
            log.exception("ingest_failed", source_id=source_id)
            raise
        _finish(session, run, "succeeded", ingested, skipped, rows)

    log.info("ingest_finished", ingested=len(ingested), skipped=len(skipped), rows=rows)
    return IngestResult(run_id=run_id, ingested=ingested, skipped=skipped, rows=rows)


def _log_skip(ref: RawFileRef, existing: RawFile, source_id: str) -> None:
    """A skip is routine for the same source; under another source it may be a mislabelled feed."""
    if existing.source_id == source_id:
        log.info("raw_file_skipped", file=str(ref.path), raw_file_id=existing.raw_file_id)
        return
    log.warning(
        "raw_file_already_ingested_under_other_source",
        file=str(ref.path),
        raw_file_id=existing.raw_file_id,
        source_id=source_id,
        existing_source_id=existing.source_id,
        detail=(
            f"identical bytes were ingested as source {existing.source_id!r}; "
            f"not recorded again under {source_id!r}"
        ),
    )


def verify_raw_store(cfg: AppConfig, engine: Engine) -> list[IntegrityProblem]:
    """Re-hash every manifest file; report missing, modified or writable files."""
    data_dir = cfg.paths.resolve(cfg.paths.data_dir)
    problems: list[IntegrityProblem] = []
    with session_factory(engine)() as session:
        for record in session.scalars(select(RawFile).order_by(RawFile.raw_file_id)):
            path = data_dir / record.path
            if not path.is_file():
                problems.append(IntegrityProblem(record.raw_file_id, path, "missing"))
                continue
            if sha256_file(path) != record.sha256:
                problems.append(IntegrityProblem(record.raw_file_id, path, "sha256 mismatch"))
            if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                problems.append(IntegrityProblem(record.raw_file_id, path, "writable"))
    return problems


def _ingest_file(
    session: Session,
    adapter: SourceAdapter,
    ref: RawFileRef,
    digest: str,
    data_dir: Path,
    source_id: str,
    instrument_id: str,
    run_id: str,
) -> RawFile:
    raw_file_id = raw_file_id_for(digest)
    stored_name = f"{raw_file_id}__{ref.original_name}"
    source_root = data_dir / RAW_DIR / source_id / instrument_id

    # Parse the stored copy, never the caller's file, so the mirror derives from immutable bytes.
    incoming = source_root / INCOMING_DIR / stored_name
    incoming.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ref.path, incoming)
    if sha256_file(incoming) != digest:
        incoming.unlink()
        raise RawStoreIntegrityError(f"{ref.path} changed while it was being copied")

    raw = adapter.read(RawFileRef(incoming, ref.original_name, ref.size))
    times = adapter.timestamps(raw)
    first = from_ns(int(times.ts_utc.min())) if len(raw) else None
    last = from_ns(int(times.ts_utc.max())) if len(raw) else None
    period = first.strftime("%Y/%m") if first is not None else "undated"
    final = source_root / period / stored_name
    _place_read_only(incoming, final, digest)

    mirror = raw.assign(raw_file_id=raw_file_id, ts_utc=times.ts_utc, ts_flags=times.flags)
    mirror = mirror[
        ["raw_file_id", "row_num", "ts_raw", "ts_utc", "ts_flags"]
        + [c for c in raw.columns if c not in ("row_num", "ts_raw")]
    ]
    _write_mirror(mirror, data_dir / MIRROR_DIR / source_id / instrument_id, raw_file_id)

    record = RawFile(
        raw_file_id=raw_file_id,
        source_id=source_id,
        instrument_id=instrument_id,
        path=final.relative_to(data_dir).as_posix(),
        original_name=ref.original_name,
        sha256=digest,
        bytes=final.stat().st_size,
        row_count=len(raw),
        first_ts_utc=first,
        last_ts_utc=last,
        ingest_run_id=run_id,
        ingested_at=utc_now(),
    )
    session.add(record)
    return record


def _place_read_only(incoming: Path, final: Path, digest: str) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        # Left by an interrupted run before its manifest row was written: adopt it if identical.
        if sha256_file(final) != digest:
            raise RawStoreIntegrityError(f"{final} exists with different content")
        incoming.unlink()
    else:
        os.replace(incoming, final)
    final.chmod(READ_ONLY)


def _write_mirror(mirror: pd.DataFrame, root: Path, raw_file_id: str) -> None:
    days = mirror["ts_utc"].to_numpy(dtype=np.int64) // _DAY_NS
    for day in np.unique(days):
        stamp = from_ns(int(day) * _DAY_NS)
        target = (
            root
            / f"year={stamp.year:04d}"
            / f"month={stamp.month:02d}"
            / f"day={stamp.day:02d}"
            / f"part-{raw_file_id}.parquet"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        part = mirror[days == day].reset_index(drop=True)
        table = pa.Table.from_pandas(part, preserve_index=False)
        temporary = target.with_suffix(".parquet.tmp")
        pq.write_table(table, temporary, compression="zstd")
        os.replace(temporary, target)


def _finish(
    session: Session,
    run: IngestRun,
    status: str,
    ingested: list[str],
    skipped: list[str],
    rows: int,
) -> None:
    run.status = status
    run.finished_at = utc_now()
    run.params_json = {
        **run.params_json,
        "ingested": len(ingested),
        "skipped": len(skipped),
        "rows": rows,
    }
    session.commit()
