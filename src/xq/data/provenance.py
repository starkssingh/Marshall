"""Recording where data comes from (DATA-004).

Before ingesting, the source and instrument declarations from config are written to the metadata
database. A source's identity — feed type, price type and clock convention — cannot change once
files have been ingested under it: files read with one clock must never be mixed with files read
with another under the same source id. Descriptive fields (vendor, venue, notes) may be updated.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from xq.core.config import InstrumentSpec, SourceConfig
from xq.core.errors import ProvenanceError
from xq.core.logging import get_logger
from xq.tracking.models import DataSource, Instrument, RawFile

_IDENTITY_FIELDS = ("feed_type", "price_type", "clock_convention")

log = get_logger(__name__)


def register_source(
    session: Session, source_id: str, source: SourceConfig, instrument: InstrumentSpec
) -> None:
    """Insert or update the `data_sources` and `instruments` rows for an ingest.

    Raises:
        ProvenanceError: if the source already has ingested files and its identity changed.
    """
    declared = {
        "vendor": source.vendor,
        "feed_type": source.feed_type,
        "venue": source.venue,
        "price_type": source.price_type,
        "clock_convention": source.clock,
        "notes": source.notes,
    }
    row = session.get(DataSource, source_id)
    if row is None:
        session.add(DataSource(source_id=source_id, **declared))
    else:
        changed = {k: (getattr(row, k), v) for k, v in declared.items() if getattr(row, k) != v}
        identity = {k: change for k, change in changed.items() if k in _IDENTITY_FIELDS}
        has_files = session.scalar(select(exists().where(RawFile.source_id == source_id)))
        if identity and has_files:
            details = ", ".join(f"{k}: {old!r} -> {new!r}" for k, (old, new) in identity.items())
            raise ProvenanceError(
                f"source {source_id!r} already has ingested files and its identity changed "
                f"({details}); declare a new source id instead"
            )
        for key, (old, new) in changed.items():
            log.info("source_updated", source_id=source_id, field=key, old=old, new=new)
            setattr(row, key, new)

    _register_instrument(session, source.instrument, source.venue, instrument)
    session.flush()


def _register_instrument(
    session: Session, instrument_id: str, venue: str, spec: InstrumentSpec
) -> None:
    terms = {
        "symbol": spec.symbol,
        "tick_size": spec.tick_size,
        "contract_size": spec.contract_size,
        "quote_ccy": spec.quote_ccy,
        "lot_step": spec.lot_step,
        "min_lot": spec.min_lot,
        "max_lot": spec.max_lot,
        "venue": venue,
    }
    row = session.get(Instrument, instrument_id)
    if row is None:
        session.add(Instrument(instrument_id=instrument_id, **terms))
        return
    for key, new in terms.items():
        old = getattr(row, key)
        if old != new:
            log.info("instrument_updated", instrument_id=instrument_id, field=key, old=old, new=new)
            setattr(row, key, new)
