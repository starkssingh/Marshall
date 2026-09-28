"""DATA-013: `xq fetch dukascopy` over real HTTP, with a local server standing in for the vendor.

No test reaches the internet: the server listens on 127.0.0.1 and serves the synthetic fixture
hours under the vendor's URL layout (months numbered from 00).
"""

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from helpers.dukascopy_fixtures import bi5_fixture_quotes, encode_bi5, hour_records
from helpers.pipeline import REPO
from xq.cli.main import app
from xq.data.adapters.dukascopy import datafeed_url
from xq.data.adapters.dukascopy_fetch import USER_AGENT, TransportError, UrllibTransport

HOURS = hour_records(bi5_fixture_quotes())
FRIDAY = "2024-03-08"


class Vendor(ThreadingHTTPServer):
    """Serves `payloads` by URL path; `/slow/...` answers after a second; records user agents."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self.server_address[1]}/datafeed"
        self.payloads = {
            datafeed_url(self.base_url, "XAUUSD", hour).removeprefix(
                f"http://127.0.0.1:{self.server_address[1]}"
            ): encode_bi5(rows)
            for hour, rows in HOURS.items()
        }
        self.user_agents: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    server: Vendor

    def do_GET(self) -> None:
        self.server.user_agents.append(self.headers.get("User-Agent", ""))
        if self.path.startswith("/slow/"):
            time.sleep(1.0)
        body = self.server.payloads.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> Iterator[Vendor]:
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    server = Vendor()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_transport_returns_bodies_statuses_and_identifies_itself(vendor: Vendor) -> None:
    transport = UrllibTransport()
    hour = pd.Timestamp("2024-03-08 10:00", tz="UTC")
    answer = transport.get(datafeed_url(vendor.base_url, "XAUUSD", hour), timeout=5)
    assert (answer.status, answer.body) == (200, encode_bi5(HOURS[hour]))
    missing = transport.get(f"{vendor.base_url}/XAUUSD/2024/02/09/12h_ticks.bi5", timeout=5)
    assert (missing.status, missing.body) == (404, b"")
    assert vendor.user_agents == [USER_AGENT, USER_AGENT]


def test_a_timeout_is_a_transport_error(vendor: Vendor) -> None:
    slow = vendor.base_url.replace("/datafeed", "/slow/datafeed")
    with pytest.raises(TransportError):
        UrllibTransport().get(f"{slow}/XAUUSD/2024/02/08/10h_ticks.bi5", timeout=0.2)


def test_cli_fetches_then_ingests(vendor: Vendor, tmp_path: Path) -> None:
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
        f"sources.dukascopy.download.base_url={vendor.base_url}",
        "--set",
        "sources.dukascopy.download.min_interval_s=0.001",
    ]
    out = tmp_path / "downloads"
    fetch = [
        *common,
        "fetch",
        "dukascopy",
        "--instrument",
        "xauusd",
        "--from",
        FRIDAY,
        "--to",
        FRIDAY,
        "--out",
        str(out),
    ]
    runner = CliRunner()
    first = runner.invoke(app, fetch)
    assert first.exit_code == 0, first.output
    fetched = len([h for h in HOURS if str(h.date()) == FRIDAY])
    assert f"{fetched} hour(s) fetched, {24 - fetched} empty (0 inside market hours)" in (
        first.stdout
    )
    assert f"{FRIDAY}: {fetched} fetched" in first.stdout
    assert f"next: xq ingest --source dukascopy --path {out / 'XAUUSD'}" in first.stdout

    again = runner.invoke(app, fetch)
    assert again.exit_code == 0, again.output
    assert "0 hour(s) fetched, 0 empty (0 inside market hours), 24 already present" in again.stdout

    ingested = runner.invoke(
        app, [*common, "ingest", "--source", "dukascopy", "--path", str(out / "XAUUSD")]
    )
    assert ingested.exit_code == 0, ingested.output
    assert f"ingested {fetched} file(s)" in ingested.stdout


def test_cli_refuses_an_instrument_the_source_does_not_carry(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "--config-dir",
            str(REPO / "config"),
            "--set",
            f"paths.root={tmp_path}",
            "--set",
            "logging.console=false",
            "fetch",
            "dukascopy",
            "--instrument",
            "eurusd",
            "--from",
            FRIDAY,
            "--to",
            FRIDAY,
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "declared for 'xauusd', not 'eurusd'" in result.output
