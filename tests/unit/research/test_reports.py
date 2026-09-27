"""EDA-001: deterministic report builds and discovery-window enforcement."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helpers.pipeline import REPO, config
from xq.core.errors import ConfigError
from xq.core.time import trading_day, trading_day_bounds
from xq.research.reports import (
    DiscoveryWindow,
    DiscoveryWindowError,
    ReportBuilder,
    markdown_table,
    new_figure,
    render_png,
    resolve_discovery_window,
)

UTC = "UTC"


def build(directory: Path) -> dict[str, str]:
    builder = ReportBuilder("A report", metadata={"dataset_id": "ds-x", "window": {"a": 1}})
    section = builder.section("first", "First section")
    section.text("Some *text*.")
    section.table(
        "numbers",
        pd.DataFrame(
            {"x": [1.0, np.nan, 1 / 3], "flag": [True, False, True], "s": ["a|b", "c", "d"]}
        ),
        caption="Numbers",
    )
    figure = new_figure()
    axes = figure.subplots()
    axes.plot(np.arange(20), np.sin(np.arange(20)))
    section.figure("wave", figure, caption="A wave")
    long = builder.section("second", "Second section")
    long.table("long", pd.DataFrame({"i": range(10)}), caption="Long", max_rows=3)
    long.table("hidden", pd.DataFrame({"i": range(5)}), caption="Hidden", max_rows=0)
    builder.attach("extra.yaml", b"a: 1\n")
    return builder.build(directory)


def test_report_build_is_deterministic(tmp_path: Path) -> None:
    first = build(tmp_path / "one")
    second = build(tmp_path / "two")
    assert first == second
    for relative in first:
        assert (tmp_path / "one" / relative).read_bytes() == (
            tmp_path / "two" / relative
        ).read_bytes()
    manifest = json.loads((tmp_path / "one" / "manifest.json").read_text())
    assert manifest["files"] == first
    assert set(first) == {
        "index.md",
        "metadata.json",
        "extra.yaml",
        "first.md",
        "second.md",
        "tables/first-numbers.csv",
        "tables/second-long.csv",
        "tables/second-hidden.csv",
        "figures/first-wave.png",
    }


def test_report_content(tmp_path: Path) -> None:
    build(tmp_path)
    index = (tmp_path / "index.md").read_text()
    assert "- **dataset_id:** ds-x" in index
    assert "[First section](first.md)" in index
    first = (tmp_path / "first.md").read_text()
    assert "| 0.3333 | yes | d |" in first
    assert "| n/a | no | c |" in first
    assert "a\\|b" in first
    assert "![A wave](figures/first-wave.png)" in first
    second = (tmp_path / "second.md").read_text()
    assert "*The first 3 of 10 rows; all are in the CSV.*" in second
    assert "Hidden** ([csv](tables/second-hidden.csv)): 5 rows, in the CSV file only." in second
    assert pd.read_csv(tmp_path / "tables" / "second-long.csv")["i"].tolist() == list(range(10))


def test_report_refuses_to_overwrite_and_duplicate_names(tmp_path: Path) -> None:
    build(tmp_path)
    with pytest.raises(FileExistsError):
        build(tmp_path)
    builder = ReportBuilder("t", metadata={})
    section = builder.section("s", "S")
    section.table("t", pd.DataFrame({"a": [1]}), caption="c")
    with pytest.raises(ValueError, match="already has a table"):
        section.table("t", pd.DataFrame({"a": [1]}), caption="c")
    with pytest.raises(ValueError, match="taken"):
        builder.section("s", "again")
    with pytest.raises(ValueError, match="taken"):
        builder.section("index", "the index")
    with pytest.raises(ValueError, match="report keys"):
        builder.section("Bad Key", "x")
    with pytest.raises(ValueError, match="taken or not a plain file name"):
        builder.attach("s.md", b"")


def test_figures_carry_no_software_or_time_metadata() -> None:
    def png() -> bytes:
        figure = new_figure()
        figure.subplots().plot([0, 1], [1, 0])
        return render_png(figure)

    first = png()
    assert first == png()
    assert b"matplotlib" not in first.lower()
    assert b"Creation" not in first


def test_markdown_table_formats_values() -> None:
    text = markdown_table(pd.DataFrame({"v": [np.inf, -np.inf, 12345.678], "b": [None, 1, 2]}))
    assert text.splitlines()[0] == "| v | b |"
    assert "| inf |" in text
    assert "| -inf |" in text
    assert "| 1.235e+04 |" in text
    assert "| n/a |" in text


def test_discovery_window_from_the_fraction_ends_at_a_trading_day_start() -> None:
    cfg = config(REPO)
    window = resolve_discovery_window(cfg, pd.Timestamp("2021-09-26T21:00:00Z"))
    vault = pd.Timestamp(cfg.vault.start)
    assert window.start == pd.Timestamp("2021-09-26T21:00:00Z")
    assert window.end == trading_day_bounds(trading_day(window.end))[0]
    halfway = window.start + (vault - window.start) * 0.5
    assert window.end <= halfway < window.end + pd.Timedelta(days=1)
    assert window.end == pd.Timestamp("2023-09-26T21:00:00Z")
    assert "first 0.5" in window.rule


def test_discovery_window_with_a_fixed_end() -> None:
    cfg = config(REPO, **{"eda.discovery.end": "2023-01-02T22:00:00Z"})
    window = resolve_discovery_window(cfg, pd.Timestamp("2021-09-26T21:00:00Z"))
    assert window.end == pd.Timestamp("2023-01-02T22:00:00Z")
    assert window.rule == "eda.discovery.end (fixed)"
    with pytest.raises(DiscoveryWindowError, match="after the discovery window ends"):
        resolve_discovery_window(cfg, pd.Timestamp("2023-06-01T00:00:00Z"))


def test_discovery_window_refuses_vault_data_and_a_late_fixed_end() -> None:
    cfg = config(REPO)
    with pytest.raises(DiscoveryWindowError, match=r"vault\.start"):
        resolve_discovery_window(cfg, pd.Timestamp("2025-10-01T00:00:00Z"))
    with pytest.raises(ConfigError, match=r"after vault\.start"):
        config(REPO, **{"eda.discovery.end": "2025-10-01T00:00:00Z"})


def test_discovery_window_refuses_post_discovery_rows() -> None:
    window = DiscoveryWindow(
        pd.Timestamp("2024-01-01", tz=UTC), pd.Timestamp("2024-02-01", tz=UTC), "test"
    )
    inside = pd.DataFrame(
        {
            "start": pd.to_datetime(["2024-01-02T00:00", "2024-01-31T23:00"], utc=True),
            "available": pd.to_datetime(["2024-01-02T01:00", "2024-02-01T00:00"], utc=True),
        }
    )
    window.check(inside, available="available", start="start")
    late = inside.assign(
        available=pd.to_datetime(["2024-01-02T00:00:00", "2024-02-01T00:00:01"], utc=True)
    )
    with pytest.raises(DiscoveryWindowError, match="refuses post-discovery data"):
        window.check(late, available="available")
    early = inside.assign(start=pd.to_datetime(["2023-12-31T00:00", "2024-01-02T00:00"], utc=True))
    with pytest.raises(DiscoveryWindowError, match="start before"):
        window.check(early, available="available", start="start")
    with pytest.raises(DiscoveryWindowError, match="refuses post-discovery data"):
        window.until(pd.Timestamp("2024-02-02", tz=UTC))
    cut = window.until(pd.Timestamp("2024-01-15", tz=UTC))
    assert cut.end == pd.Timestamp("2024-01-15", tz=UTC)
    with pytest.raises(ValueError, match="tz-aware"):
        window.check(
            pd.DataFrame({"available": pd.to_datetime(["2024-01-02"])}), available="available"
        )
