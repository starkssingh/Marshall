"""`xq fetch dukascopy`: a polite, resumable downloader of Dukascopy hourly tick files (ADR 0057).

It runs on a machine with internet access (the research sandbox has none). For every UTC hour of
the days `first` to `last` (inclusive) it asks the vendor for the hour's ``.bi5`` file and keeps
the bytes unchanged at::

    <out>/<SYMBOL>/<yyyy>/<mm>/<dd>/<SYMBOL>_<yyyy-mm-dd>_<HH>h_ticks.bi5

which `xq ingest --source dukascopy --path <out>/<SYMBOL>` then reads.

- **Checksummed.** ``<out>/<SYMBOL>/manifest.jsonl`` gets one JSON line per hour: status (``ok``
  with the file's SHA-256, size and record count, or ``empty``), URL, HTTP status and fetch time.
  A payload is kept only if it decodes into whole records inside its hour.
- **Resumable.** Hours in the manifest are not requested again. The files of the requested days
  are re-hashed against it first, and a mismatch stops the run: nothing is repaired. A file
  without a manifest line (a run stopped between the two writes) is adopted after the same
  decoding check, and a partial last manifest line (a run stopped mid-write) is cut off, so its
  hour is fetched again. A lock file keeps a second downloader out of the same folder.
- **Never overwrites.** A payload is written to a hidden ``.part`` file and hard-linked to its
  final name only if that name does not exist yet.
- **Polite.** One request at a time, at least `DownloadConfig.min_interval_s` apart, retries with
  exponential backoff on network errors, timeouts, HTTP 429 and 5xx; the run stops after
  `max_attempts` failures of one hour, or at once on any other HTTP error.
- **Empty hours.** A 404 or an empty body is an hour without ticks: recorded ``empty``, no file.
  Inside the calendar's market hours an empty answer is suspect — since July 2026 the endpoint
  has been reported to time out or answer nothing (ADR 0057) — so the hour is asked again once
  after a pause, and it is recorded only once a later hour of the run brings ticks (the feed was
  answering). Empty market hours after the run's last hour with ticks are left unrecorded and
  asked again next time, and `max_empty_open_hours` of them in a row stop the run.
  ``retry_empty`` asks again for hours recorded empty.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

import xq
from xq.core.config import AppConfig, DownloadConfig
from xq.core.errors import ConfigError, SourceFormatError, XQError
from xq.core.logging import get_logger
from xq.core.time import utc_now
from xq.data.adapters.dukascopy import bi5_file_name, datafeed_url, decode_bi5
from xq.data.calendar import MarketCalendar

MANIFEST = "manifest.jsonl"
LOCK = ".fetch.lock"
USER_AGENT = f"xq-fetch/{xq.__version__} (research downloader; one request at a time)"
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_HOUR = pd.Timedelta(hours=1)

log = get_logger(__name__)


class FetchError(XQError):
    """The download stopped; rerunning resumes where it stopped."""


class DownloadIntegrityError(XQError):
    """A downloaded file differs from its manifest line, or cannot be decoded; never repaired."""


class TransportError(Exception):
    """A request failed before an HTTP status was received (network error or timeout)."""


@dataclass(frozen=True)
class Response:
    """An HTTP answer: its status and body."""

    status: int
    body: bytes


class Transport(Protocol):
    """Makes one GET request."""

    def get(self, url: str, timeout: float) -> Response:
        """Return the answer to a GET of `url`; raise `TransportError` if none came."""
        ...


class UrllibTransport:
    """`Transport` over the standard library, identifying itself with `USER_AGENT`."""

    def get(self, url: str, timeout: float) -> Response:
        """GET `url`; HTTP errors are answers, network errors and timeouts `TransportError`."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                return Response(int(answer.status), answer.read())
        except urllib.error.HTTPError as exc:
            return Response(int(exc.code), b"")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(str(exc)) from exc


@dataclass
class FetchResult:
    """What one download run did, in hours.

    `empty_in_market` counts recorded empty hours inside the calendar's market hours (holidays or
    gaps at the vendor); `unconfirmed_empty` counts empty market hours after the run's last hour
    with ticks, which are not recorded and are asked for again by the next run.
    """

    fetched: int = 0
    empty: int = 0
    empty_in_market: int = 0
    already_present: int = 0
    adopted: int = 0
    unconfirmed_empty: int = 0
    requests: int = 0
    folder: Path = field(default_factory=Path)

    @property
    def manifest(self) -> Path:
        """The run's manifest file."""
        return self.folder / MANIFEST


@dataclass(frozen=True)
class DayProgress:
    """Hours of one UTC day, reported after the day is done."""

    day: date
    fetched: int
    empty: int
    already_present: int


@dataclass
class _Clock:
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    last_request: float | None = None

    def wait_turn(self, interval: float) -> None:
        if self.last_request is not None:
            remaining = self.last_request + interval - self.monotonic()
            if remaining > 0:
                self.sleep(remaining)
        self.last_request = self.monotonic()


def fetch_dukascopy(
    cfg: AppConfig,
    source_id: str,
    first: date,
    last: date,
    out: Path,
    *,
    retry_empty: bool = False,
    transport: Transport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    today: date | None = None,
    on_day: Callable[[DayProgress], None] | None = None,
) -> FetchResult:
    """Download the hourly tick files of `source_id` for UTC days `first` to `last` into `out`.

    Args:
        cfg: Configuration; the source must use the ``dukascopy_ticks`` adapter and declare
            ``download``.
        source_id: Configured source, e.g. ``dukascopy``.
        first, last: Inclusive UTC days. `last` must be before `today` (a day still in progress
            is incomplete) and `first` on or after the vendor's history start.
        out: Output directory; files go below ``out/<SYMBOL>/``.
        retry_empty: Ask again for hours the manifest records as empty.
        transport, sleep, monotonic, today: Injected for tests; the defaults are the real ones.
        on_day: Called with each day's counts when the day is done.
    """
    source = cfg.source(source_id)
    settings = source.download
    if source.adapter != "dukascopy_ticks" or settings is None or source.vendor_symbol is None:
        raise ConfigError(f"source {source_id!r} has no Dukascopy downloader (adapter, download)")
    today = today or utc_now().date()
    if last < first:
        raise ConfigError(f"--to {last} is before --from {first}")
    if last >= today:
        raise ConfigError(f"--to {last} must be before today ({today} UTC): its hours are not over")
    if first < settings.history_start:
        raise ConfigError(
            f"--from {first} is before the vendor's first tick day {settings.history_start}"
        )

    symbol = source.vendor_symbol
    folder = out / symbol
    folder.mkdir(parents=True, exist_ok=True)
    fetcher = _Fetcher(settings, transport or UrllibTransport(), _Clock(sleep, monotonic))
    market = _MarketHours(cfg, first, last)
    result = FetchResult(folder=folder)
    with _lock(folder):
        manifest = _Manifest(folder / MANIFEST)
        log.info(
            "fetch_started",
            source_id=source_id,
            first=str(first),
            last=str(last),
            folder=str(folder),
            known_hours=len(manifest.entries),
        )
        streak: list[dict[str, Any]] = []
        day = first
        while day <= last:
            before = (result.fetched, result.empty, result.already_present + result.adopted)
            for hour in _hours(day):
                entry = manifest.entries.get(_hour_key(hour))
                path = folder / _relative_path(symbol, hour)
                if entry is not None:
                    _verify(entry, path, folder)
                    if entry["status"] == "ok" or not retry_empty:
                        result.already_present += 1
                        continue
                elif path.exists():
                    manifest.append(_adopt(path, hour, folder))
                    result.adopted += 1
                    continue
                record, body = fetcher.fetch(symbol, hour, in_market=market.is_open(hour))
                if record["status"] == "ok":
                    _place(path, body)
                    # Ticks after them show the feed was answering: the streak's empties are real.
                    manifest.extend([*streak, record])
                    result.empty += len(streak)
                    result.empty_in_market += len(streak)
                    streak.clear()
                    result.fetched += 1
                elif record["in_market"]:
                    streak.append(record)
                    if len(streak) >= settings.max_empty_open_hours:
                        raise FetchError(
                            _streak_message(streak, source_id, settings.max_empty_open_hours)
                        )
                else:
                    manifest.append(record)
                    result.empty += 1
            if on_day is not None:
                now = (result.fetched, result.empty, result.already_present + result.adopted)
                on_day(DayProgress(day, *(a - b for a, b in zip(now, before, strict=True))))
            day += timedelta(days=1)
        # Empty market hours after the last hour with ticks are not confirmed by anything: they
        # are not recorded, so the next run asks for them again.
        result.unconfirmed_empty = len(streak)
        result.requests = fetcher.requests
    log.info(
        "fetch_finished",
        fetched=result.fetched,
        empty=result.empty,
        empty_in_market=result.empty_in_market,
        already_present=result.already_present,
        adopted=result.adopted,
        unconfirmed_empty=result.unconfirmed_empty,
        requests=result.requests,
    )
    return result


class _Fetcher:
    """Requests one hour at a time with the configured pacing, retries and empty re-check."""

    def __init__(self, settings: DownloadConfig, transport: Transport, clock: _Clock) -> None:
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self.requests = 0

    def fetch(
        self, symbol: str, hour: pd.Timestamp, *, in_market: bool
    ) -> tuple[dict[str, Any], bytes]:
        """The manifest record of `hour` and the payload (empty for an hour without ticks)."""
        url = datafeed_url(self._settings.base_url, symbol, hour)
        answer = self._get(url)
        if not answer.body and in_market and self._settings.empty_retry_pause_s > 0:
            self._clock.sleep(self._settings.empty_retry_pause_s)
            answer = self._get(url)
        record: dict[str, Any] = {
            "hour": _hour_key(hour),
            "url": url,
            "http_status": answer.status,
            "in_market": in_market,
            "fetched_at": utc_now().isoformat(),
        }
        if not answer.body:
            empty = {"status": "empty", "file": None, "bytes": 0, "sha256": None, "records": 0}
            return {**record, **empty}, b""
        try:
            records = len(decode_bi5(answer.body))
        except SourceFormatError as exc:
            raise FetchError(f"{url}: the vendor sent an unreadable file ({exc})") from exc
        stored = {
            "status": "ok",
            "file": _relative_path(symbol, hour).as_posix(),
            "bytes": len(answer.body),
            "sha256": hashlib.sha256(answer.body).hexdigest(),
            "records": records,
        }
        return {**record, **stored}, answer.body

    def _get(self, url: str) -> Response:
        settings = self._settings
        problem = ""
        for attempt in range(1, settings.max_attempts + 1):
            self._clock.wait_turn(settings.min_interval_s)
            self.requests += 1
            try:
                answer = self._transport.get(url, settings.timeout_s)
            except TransportError as exc:
                problem = f"no answer ({exc})"
            else:
                if answer.status == 200:
                    return answer
                if answer.status == 404:
                    return Response(404, b"")
                if answer.status not in _RETRY_STATUSES:
                    raise FetchError(f"{url}: HTTP {answer.status}; stopped (rerun to resume)")
                problem = f"HTTP {answer.status}"
            log.warning("fetch_attempt_failed", url=url, attempt=attempt, problem=problem)
            if attempt < settings.max_attempts:
                self._clock.sleep(settings.backoff_s * 2 ** (attempt - 1))
        raise FetchError(
            f"{url}: {problem} after {settings.max_attempts} attempts; stopped (rerun to "
            "resume). Since July 2026 this endpoint has been reported to time out: if it keeps "
            "failing, use the dukascopy-node CSV route in the README (ADR 0057)."
        )


class _Manifest:
    """The append-only JSON-lines manifest: the latest line of an hour is its state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if not path.exists():
            return
        text = path.read_bytes().decode("utf-8")  # bytes: no newline translation on Windows
        if text and not text.endswith("\n"):
            # A run stopped in the middle of its last write. A line counts only once complete,
            # so the fragment records nothing: cut it off (that hour is fetched again), or the
            # next line would be appended onto it.
            complete = text[: text.rfind("\n") + 1]
            log.warning(
                "manifest_partial_line_removed", path=str(path), fragment=text[len(complete) :]
            )
            with path.open("r+b") as handle:
                handle.truncate(len(complete.encode("utf-8")))
            text = complete
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DownloadIntegrityError(f"{path}:{number} is not valid JSON") from exc
            if not isinstance(entry, dict) or not {"hour", "status"} <= entry.keys():
                raise DownloadIntegrityError(f"{path}:{number} is not a manifest record")
            previous = self.entries.get(entry["hour"])
            if previous is not None and previous["status"] == "ok" and entry["status"] != "ok":
                continue  # an hour with ticks stays an hour with ticks
            self.entries[entry["hour"]] = entry

    def append(self, entry: dict[str, Any]) -> None:
        self.extend([entry])

    def extend(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        text = "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries)
        with self.path.open("ab") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        for entry in entries:
            self.entries[entry["hour"]] = entry


class _MarketHours:
    """Whether an hour overlaps the calendar's market hours (config/sessions.yaml)."""

    def __init__(self, cfg: AppConfig, first: date, last: date) -> None:
        calendar = MarketCalendar.for_range(cfg.sessions_config(), first, last + timedelta(1))
        intervals = []
        day = first
        while day <= last + timedelta(days=1):
            status = calendar.status(day)
            if status.market_open_utc is not None and status.market_close_utc is not None:
                intervals.append((status.market_open_utc, status.market_close_utc))
            day += timedelta(days=1)
        self._intervals = intervals

    def is_open(self, hour: pd.Timestamp) -> bool:
        end = hour + _HOUR
        return any(start < end and hour < stop for start, stop in self._intervals)


def _hours(day: date) -> Iterator[pd.Timestamp]:
    start = pd.Timestamp(day.year, day.month, day.day, tz="UTC")
    for offset in range(24):
        yield start + offset * _HOUR


def _hour_key(hour: pd.Timestamp) -> str:
    return f"{hour:%Y-%m-%dT%H}:00:00Z"


def _relative_path(symbol: str, hour: pd.Timestamp) -> Path:
    return Path(f"{hour:%Y}", f"{hour:%m}", f"{hour:%d}", bi5_file_name(symbol, hour))


def _verify(entry: dict[str, Any], path: Path, folder: Path) -> None:
    """A manifest `ok` hour's file must exist with the recorded hash; an empty one has none."""
    if entry["status"] != "ok":
        if path.exists():
            raise DownloadIntegrityError(f"{path} exists but the manifest records the hour empty")
        return
    if not path.exists():
        raise DownloadIntegrityError(
            f"{path} is in the manifest ({folder / MANIFEST}) but missing; restore it, or remove "
            "its manifest line to fetch it again"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        raise DownloadIntegrityError(f"{path} does not match its manifest SHA-256; not repaired")


def _adopt(path: Path, hour: pd.Timestamp, folder: Path) -> dict[str, Any]:
    """A manifest line for a file a stopped run placed but did not record."""
    body = path.read_bytes()
    try:
        records = len(decode_bi5(body))
    except SourceFormatError as exc:
        raise DownloadIntegrityError(
            f"{path} has no manifest line and cannot be decoded ({exc}); delete it and rerun"
        ) from exc
    log.info("fetch_file_adopted", path=str(path))
    return {
        "hour": _hour_key(hour),
        "status": "ok",
        "file": path.relative_to(folder).as_posix(),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "records": records,
        "adopted": True,
        "fetched_at": utc_now().isoformat(),
    }


def _place(path: Path, body: bytes) -> None:
    """Write `body` to `path` through a hidden temporary file, never replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.part")
    with partial.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(partial, path)
    except FileExistsError as exc:
        partial.unlink()
        raise DownloadIntegrityError(f"{path} appeared during the download; not replaced") from exc
    except OSError:  # a file system without hard links: exclusive create instead
        with path.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    partial.unlink()


@contextmanager
def _lock(folder: Path) -> Iterator[None]:
    lock = folder / LOCK
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise FetchError(
            f"{lock} exists: another download may be writing to {folder}. If none is running "
            "(a previous run was killed), delete the lock file and rerun."
        ) from exc
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"{os.getpid()}\n")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _streak_message(streak: list[dict[str, Any]], source_id: str, limit: int) -> str:
    return (
        f"{len(streak)} consecutive hours inside market hours came back empty "
        f"({streak[0]['hour']} to {streak[-1]['hour']}); stopped without recording them. The "
        "endpoint is more likely failing or throttling than the market silent: wait and rerun. "
        "If the vendor really has no ticks for that period, rerun with --set "
        f"sources.{source_id}.download.max_empty_open_hours=<more than {limit}>."
    )
