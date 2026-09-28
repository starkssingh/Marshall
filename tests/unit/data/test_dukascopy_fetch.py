"""DATA-013: `xq fetch dukascopy` is resumable, checksummed, polite and never overwrites a file."""

import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.dukascopy_fixtures import bi5_fixture_quotes, encode_bi5, hour_records
from xq.core.config import AppConfig, load_config
from xq.core.errors import ConfigError
from xq.data.adapters import build_adapter
from xq.data.adapters.dukascopy import datafeed_url, decode_bi5
from xq.data.adapters.dukascopy_fetch import (
    LOCK,
    MANIFEST,
    DayProgress,
    DownloadIntegrityError,
    FetchError,
    FetchResult,
    Response,
    TransportError,
    fetch_dukascopy,
)

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
TODAY = date(2026, 9, 28)
FRIDAY, MONDAY = date(2024, 3, 8), date(2024, 3, 11)  # the weekend and the US DST change
HOURS = hour_records(bi5_fixture_quotes())
PAYLOADS = {hour: encode_bi5(rows) for hour, rows in HOURS.items()}


def in_range(first: date, last: date) -> dict[pd.Timestamp, bytes]:
    return {h: b for h, b in PAYLOADS.items() if first <= h.date() <= last}


class FakeVendor:
    """Serves the fixture hours by URL; other hours are empty. Records every request."""

    def __init__(
        self,
        cfg: AppConfig,
        payloads: dict[pd.Timestamp, bytes] | None = None,
        *,
        empty_status: int = 200,
        down: bool = False,
    ) -> None:
        base = cfg.source("dukascopy").download
        assert base is not None
        self.urls = {
            datafeed_url(base.base_url, "XAUUSD", hour): body
            for hour, body in (PAYLOADS if payloads is None else payloads).items()
        }
        self.empty_status = empty_status
        self.down = down
        self.scripted: dict[str, list[Response | Exception]] = {}
        self.requests: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        self.requests.append(url)
        if self.scripted.get(url):
            outcome = self.scripted[url].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if self.down:
            raise TransportError("timed out")
        body = self.urls.get(url, b"")
        return Response(200, body) if body else Response(self.empty_status, b"")


class FakeClock:
    """Time passes only when the downloader sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_config("research", config_dir=REPO_CONFIG)


def fetch(
    cfg: AppConfig,
    vendor: FakeVendor,
    out: Path,
    first: date = FRIDAY,
    last: date = MONDAY,
    clock: FakeClock | None = None,
    **kwargs: object,
) -> FetchResult:
    clock = clock or FakeClock()
    return fetch_dukascopy(
        cfg,
        "dukascopy",
        first,
        last,
        out,
        transport=vendor,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        today=TODAY,
        **kwargs,  # type: ignore[arg-type]
    )


def manifest(out: Path) -> dict[str, dict[str, object]]:
    lines = (out / "XAUUSD" / MANIFEST).read_text().splitlines()
    return {entry["hour"]: entry for entry in map(json.loads, lines)}


def key(hour: pd.Timestamp) -> str:
    return f"{hour:%Y-%m-%dT%H}:00:00Z"


def test_every_hour_is_fetched_once_and_checksummed(cfg: AppConfig, tmp_path: Path) -> None:
    vendor = FakeVendor(cfg)
    days: list[DayProgress] = []
    result = fetch(cfg, vendor, tmp_path, on_day=days.append)

    expected = in_range(FRIDAY, MONDAY)
    folder = tmp_path / "XAUUSD"
    files = sorted(folder.rglob("*.bi5"))
    assert [p.name for p in files] == [f"XAUUSD_{h:%Y-%m-%d_%H}h_ticks.bi5" for h in expected]
    assert files[0].relative_to(folder).parts[:3] == ("2024", "03", "08")
    for path, body in zip(files, expected.values(), strict=True):
        assert path.read_bytes() == body  # the vendor's bytes, unchanged

    entries = manifest(tmp_path)
    assert len(entries) == 4 * 24  # every hour of the four days, weekend included
    for hour, body in expected.items():
        entry = entries[key(hour)]
        assert entry["status"] == "ok"
        assert entry["sha256"] == hashlib.sha256(body).hexdigest()
        assert entry["records"] == len(HOURS[hour])
    saturday_noon = entries["2024-03-09T12:00:00Z"]
    assert (saturday_noon["status"], saturday_noon["in_market"]) == ("empty", False)
    assert result.fetched == len(expected)
    assert result.empty == 96 - len(expected)
    assert result.empty_in_market == 0  # every market hour of the fixture has ticks
    assert result.requests == 96  # no empty hour inside market hours: nothing asked twice
    assert [d.day for d in days] == [date(2024, 3, d) for d in (8, 9, 10, 11)]
    assert days[1] == DayProgress(date(2024, 3, 9), 0, 24, 0)  # Saturday
    assert not (folder / LOCK).exists()


def test_urls_number_months_from_zero(cfg: AppConfig, tmp_path: Path) -> None:
    vendor = FakeVendor(cfg)
    fetch(cfg, vendor, tmp_path, first=MONDAY, last=MONDAY)
    assert (
        vendor.requests[0]
        == "https://datafeed.dukascopy.com/datafeed/XAUUSD/2024/02/11/00h_ticks.bi5"
    )


def test_the_files_ingest_as_the_dukascopy_source(cfg: AppConfig, tmp_path: Path) -> None:
    fetch(cfg, FakeVendor(cfg), tmp_path)
    adapter = build_adapter(cfg, "dukascopy")
    refs = adapter.discover(tmp_path / "XAUUSD")
    assert len(refs) == len(in_range(FRIDAY, MONDAY))  # the manifest and lock are not data
    ticks = pd.concat([adapter.to_canonical(adapter.read(r)) for r in refs])
    assert len(ticks) == sum(len(HOURS[h]) for h in in_range(FRIDAY, MONDAY))


def test_a_rerun_verifies_and_requests_nothing(cfg: AppConfig, tmp_path: Path) -> None:
    fetch(cfg, FakeVendor(cfg), tmp_path)
    before = {p: p.stat().st_mtime_ns for p in (tmp_path / "XAUUSD").rglob("*")}
    vendor = FakeVendor(cfg)
    result = fetch(cfg, vendor, tmp_path)
    assert vendor.requests == []
    assert (result.fetched, result.already_present) == (0, 96)
    assert {p: p.stat().st_mtime_ns for p in (tmp_path / "XAUUSD").rglob("*")} == before


def test_a_modified_file_stops_the_run_and_is_not_repaired(cfg: AppConfig, tmp_path: Path) -> None:
    fetch(cfg, FakeVendor(cfg), tmp_path)
    victim = sorted((tmp_path / "XAUUSD").rglob("*.bi5"))[3]
    victim.write_bytes(b"tampered")
    with pytest.raises(DownloadIntegrityError, match="does not match its manifest SHA-256"):
        fetch(cfg, FakeVendor(cfg), tmp_path)
    assert victim.read_bytes() == b"tampered"


def test_a_missing_file_listed_in_the_manifest_stops_the_run(
    cfg: AppConfig, tmp_path: Path
) -> None:
    fetch(cfg, FakeVendor(cfg), tmp_path)
    sorted((tmp_path / "XAUUSD").rglob("*.bi5"))[0].unlink()
    with pytest.raises(DownloadIntegrityError, match="missing"):
        fetch(cfg, FakeVendor(cfg), tmp_path)


def test_an_unrecorded_file_is_adopted_never_overwritten(cfg: AppConfig, tmp_path: Path) -> None:
    hour = pd.Timestamp("2024-03-08 10:00", tz="UTC")
    path = tmp_path / "XAUUSD" / "2024" / "03" / "08" / "XAUUSD_2024-03-08_10h_ticks.bi5"
    path.parent.mkdir(parents=True)
    path.write_bytes(PAYLOADS[hour])  # placed by a run that stopped before its manifest line
    vendor = FakeVendor(cfg)
    result = fetch(cfg, vendor, tmp_path, first=FRIDAY, last=FRIDAY)
    assert result.adopted == 1
    assert datafeed_url("https://datafeed.dukascopy.com/datafeed", "XAUUSD", hour) not in (
        vendor.requests
    )
    assert manifest(tmp_path)[key(hour)]["adopted"] is True

    other = path.with_name("XAUUSD_2024-03-08_11h_ticks.bi5")
    other.unlink()
    lines = (tmp_path / "XAUUSD" / MANIFEST).read_text().splitlines()
    kept = [line for line in lines if json.loads(line)["hour"] != "2024-03-08T11:00:00Z"]
    (tmp_path / "XAUUSD" / MANIFEST).write_text("\n".join(kept) + "\n")
    other.write_bytes(b"half a download")
    with pytest.raises(DownloadIntegrityError, match="cannot be decoded"):
        fetch(cfg, FakeVendor(cfg), tmp_path, first=FRIDAY, last=FRIDAY)
    assert other.read_bytes() == b"half a download"


def test_an_interrupted_run_resumes_where_it_stopped(cfg: AppConfig, tmp_path: Path) -> None:
    broken = FakeVendor(cfg)
    stop_at = datafeed_url(
        "https://datafeed.dukascopy.com/datafeed",
        "XAUUSD",
        pd.Timestamp("2024-03-08 12:00", tz="UTC"),
    )
    broken.scripted[stop_at] = [TransportError("reset")] * 4
    with pytest.raises(FetchError, match="after 4 attempts"):
        fetch(cfg, broken, tmp_path)
    assert len(manifest(tmp_path)) == 12  # 00:00 to 11:00 recorded
    assert not (tmp_path / "XAUUSD" / LOCK).exists()

    vendor = FakeVendor(cfg)
    result = fetch(cfg, vendor, tmp_path)
    assert result.already_present == 12
    assert len(vendor.requests) == 96 - 12
    assert len(manifest(tmp_path)) == 96


def test_failed_requests_are_retried_with_backoff(cfg: AppConfig, tmp_path: Path) -> None:
    vendor = FakeVendor(cfg)
    first_hour = pd.Timestamp("2024-03-08 00:00", tz="UTC")
    url = datafeed_url("https://datafeed.dukascopy.com/datafeed", "XAUUSD", first_hour)
    vendor.scripted[url] = [TransportError("timed out"), Response(503, b"")]
    clock = FakeClock()
    result = fetch(cfg, vendor, tmp_path, first=FRIDAY, last=FRIDAY, clock=clock)
    assert vendor.requests[:3] == [url, url, url]
    assert clock.sleeps[:2] == [2.0, 4.0]  # backoff doubles
    assert result.fetched == len(in_range(FRIDAY, FRIDAY))
    assert (tmp_path / "XAUUSD" / "2024" / "03" / "08" / "XAUUSD_2024-03-08_00h_ticks.bi5").exists()


def test_a_dead_endpoint_stops_the_run_with_the_fallback(cfg: AppConfig, tmp_path: Path) -> None:
    vendor = FakeVendor(cfg, down=True)
    with pytest.raises(FetchError, match="dukascopy-node CSV route") as caught:
        fetch(cfg, vendor, tmp_path)
    assert "no answer (timed out) after 4 attempts" in str(caught.value)
    assert len(vendor.requests) == 4
    assert not (tmp_path / "XAUUSD" / MANIFEST).exists()


def test_other_http_errors_stop_at_once(cfg: AppConfig, tmp_path: Path) -> None:
    vendor = FakeVendor(cfg, payloads={}, empty_status=403)
    with pytest.raises(FetchError, match="HTTP 403"):
        fetch(cfg, vendor, tmp_path)
    assert len(vendor.requests) == 1


def test_requests_are_paced_one_at_a_time(cfg: AppConfig, tmp_path: Path) -> None:
    clock = FakeClock()
    times: list[float] = []

    class Timed(FakeVendor):
        def get(self, url: str, timeout: float) -> Response:
            times.append(clock.now)
            assert timeout == 30.0
            return super().get(url, timeout)

    fetch(cfg, Timed(cfg), tmp_path, first=FRIDAY, last=FRIDAY, clock=clock)
    assert len(times) == 24
    assert np.diff(times).min() >= 0.5


def test_404_is_an_hour_without_ticks(cfg: AppConfig, tmp_path: Path) -> None:
    result = fetch(cfg, FakeVendor(cfg, empty_status=404), tmp_path)
    assert result.fetched == len(in_range(FRIDAY, MONDAY))
    assert manifest(tmp_path)["2024-03-09T12:00:00Z"]["http_status"] == 404


def test_empty_market_hours_are_asked_twice_and_recorded_once_confirmed(
    cfg: AppConfig, tmp_path: Path
) -> None:
    gap = [pd.Timestamp(f"2024-03-08 {h:02d}:00", tz="UTC") for h in (10, 11, 12)]
    vendor = FakeVendor(cfg, {h: b for h, b in PAYLOADS.items() if h not in gap})
    clock = FakeClock()
    result = fetch(cfg, vendor, tmp_path, first=FRIDAY, last=FRIDAY, clock=clock)
    entries = manifest(tmp_path)
    for hour in gap:
        entry = entries[key(hour)]
        assert (entry["status"], entry["in_market"]) == ("empty", True)
        url = datafeed_url("https://datafeed.dukascopy.com/datafeed", "XAUUSD", hour)
        assert vendor.requests.count(url) == 2
    assert clock.sleeps.count(5.0) == 3
    assert (result.empty_in_market, result.unconfirmed_empty) == (3, 0)


def test_trailing_empty_market_hours_are_not_recorded(cfg: AppConfig, tmp_path: Path) -> None:
    tail = [pd.Timestamp(f"2024-03-08 {h:02d}:00", tz="UTC") for h in range(18, 22)]
    vendor = FakeVendor(cfg, {h: b for h, b in PAYLOADS.items() if h not in tail})
    result = fetch(cfg, vendor, tmp_path, first=FRIDAY, last=FRIDAY)
    assert result.unconfirmed_empty == 4
    entries = manifest(tmp_path)
    assert not [h for h in tail if key(h) in entries]
    assert entries["2024-03-08T22:00:00Z"]["status"] == "empty"  # market closed: recorded

    again = FakeVendor(cfg)
    fetch(cfg, again, tmp_path, first=FRIDAY, last=FRIDAY)
    assert len(again.requests) == 4  # only the unconfirmed hours are asked again
    assert all(manifest(tmp_path)[key(h)]["status"] == "ok" for h in tail)


def test_an_endpoint_answering_nothing_stops_the_run(cfg: AppConfig, tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="24 consecutive hours inside market hours"):
        fetch(cfg, FakeVendor(cfg, payloads={}), tmp_path)
    entries = manifest(tmp_path)
    assert entries  # hours outside market hours are recorded
    assert not [e for e in entries.values() if e["in_market"]]


def test_retry_empty_asks_again_for_recorded_empty_hours(cfg: AppConfig, tmp_path: Path) -> None:
    gap = pd.Timestamp("2024-03-08 10:00", tz="UTC")
    fetch(cfg, FakeVendor(cfg, {h: b for h, b in PAYLOADS.items() if h != gap}), tmp_path)
    assert manifest(tmp_path)[key(gap)]["status"] == "empty"
    vendor = FakeVendor(cfg)
    result = fetch(cfg, vendor, tmp_path, retry_empty=True)
    assert result.fetched == 1
    assert manifest(tmp_path)[key(gap)]["status"] == "ok"  # the latest line wins
    assert len(decode_bi5(next((tmp_path / "XAUUSD").rglob("*08_10h_ticks.bi5")).read_bytes()))


def test_a_partial_last_manifest_line_is_fetched_again(cfg: AppConfig, tmp_path: Path) -> None:
    fetch(cfg, FakeVendor(cfg), tmp_path, first=FRIDAY, last=FRIDAY)
    path = tmp_path / "XAUUSD" / MANIFEST
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:20])  # a run killed mid-write
    vendor = FakeVendor(cfg)
    fetch(cfg, vendor, tmp_path, first=FRIDAY, last=FRIDAY)
    assert len(vendor.requests) == 1
    rewritten = path.read_text().splitlines()
    assert [json.loads(line) for line in rewritten[:-1]] == [json.loads(x) for x in lines[:-1]]
    assert json.loads(rewritten[-1])["hour"] == json.loads(lines[-1])["hour"]


def test_a_corrupt_manifest_line_stops_the_run(cfg: AppConfig, tmp_path: Path) -> None:
    fetch(cfg, FakeVendor(cfg), tmp_path, first=FRIDAY, last=FRIDAY)
    path = tmp_path / "XAUUSD" / MANIFEST
    lines = path.read_text().splitlines()
    lines[3] = "{not json"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(DownloadIntegrityError, match=f"{MANIFEST}:4 is not valid JSON"):
        fetch(cfg, FakeVendor(cfg), tmp_path, first=FRIDAY, last=FRIDAY)


def test_a_second_downloader_is_kept_out(cfg: AppConfig, tmp_path: Path) -> None:
    lock = tmp_path / "XAUUSD" / LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text("12345\n")
    with pytest.raises(FetchError, match="another download may be writing"):
        fetch(cfg, FakeVendor(cfg), tmp_path)
    assert lock.exists()  # not ours to remove


@pytest.mark.parametrize(
    ("first", "last", "message"),
    [
        (date(2024, 3, 11), date(2024, 3, 8), "before --from"),
        (date(2026, 9, 27), TODAY, "must be before today"),
        (date(2003, 5, 4), date(2003, 5, 6), "first tick day 2003-05-05"),
    ],
)
def test_impossible_ranges_are_refused(
    cfg: AppConfig, tmp_path: Path, first: date, last: date, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        fetch(cfg, FakeVendor(cfg), tmp_path, first=first, last=last)


def test_only_a_dukascopy_source_can_be_fetched(cfg: AppConfig, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no Dukascopy downloader"):
        fetch_dukascopy(cfg, "mt5_primary", FRIDAY, MONDAY, tmp_path, today=TODAY)
