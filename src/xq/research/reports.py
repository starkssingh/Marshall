"""Research reports and discovery-window enforcement (EDA-001).

**Reports.** `ReportBuilder` turns results computed by library code into a versioned report
directory; nothing in a report is written by hand or by a notebook (an optional Jupytext notebook
may only import library code and call it). A report has sections; each section is one Markdown
document with text, tables and figures:

- ``index.md`` — the title, the provenance (dataset, discovery window, git sha, configuration
  hashes) and links to the sections;
- ``<section>.md`` — the section's text, its tables rendered as Markdown and its figures;
- ``tables/<section>-<name>.csv`` — every table in full precision, for machine reading;
- ``figures/<section>-<name>.png`` — every figure, rendered by matplotlib's Agg canvas without
  timestamps or software metadata, so the bytes depend only on the data and the library versions;
- ``metadata.json`` — the provenance given to the builder (dataset id, window, git sha, config);
- ``manifest.json`` — the SHA-256 of every file above.

The build is **deterministic**: the same results, metadata and library versions give the same
bytes. Anything that differs between runs of the same inputs — the run id, the start time — belongs
in the run registry (or in a ``run.json`` written by the caller), never in these files.

**Discovery window.** Exploratory research reads only the discovery window (plan section 2: the
first 50-60 % of the non-vault data), so hypotheses found by looking at data are tested on later
data. `resolve_discovery_window` derives it from ``config/eda.yaml``: the first
``discovery.fraction`` of the span from the dataset's start to ``vault.start``, ending at the start
of a trading day (17:00 New York), or everything before ``discovery.end`` once that is fixed.
`DiscoveryWindow.check` refuses a frame holding anything that becomes available after the window
ends, and a request reaching past it raises `DiscoveryWindowError` rather than being cut silently.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from xq.core.config import AppConfig
from xq.core.errors import NaiveTimestampError, XQError
from xq.core.time import TimestampLike, ensure_utc, trading_day, trading_day_bounds
from xq.datasets.vault import vault_start

INDEX_FILE = "index.md"
METADATA_FILE = "metadata.json"
MANIFEST_FILE = "manifest.json"
#: Run-specific record (run id, confirmatory flag) written next to a report, outside its manifest.
RUN_FILE = "run.json"
TABLES_DIR = "tables"
FIGURES_DIR = "figures"
#: Resolution of every figure; part of the deterministic output.
FIGURE_DPI = 100
_KEY = "abcdefghijklmnopqrstuvwxyz0123456789_-"


class DiscoveryWindowError(XQError):
    """Exploratory research asked for data outside the discovery window."""


@dataclass(frozen=True)
class DiscoveryWindow:
    """The data exploratory research may read: everything available in ``[start, end]``.

    A row belongs to the window if it starts at or after `start` and becomes available at or before
    `end` (a bar that starts before `end` but completes after it is outside). `rule` says how the
    window was set.
    """

    start: pd.Timestamp
    end: pd.Timestamp
    rule: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start))
        object.__setattr__(self, "end", ensure_utc(self.end))
        if self.end <= self.start:
            raise DiscoveryWindowError(
                f"the discovery window must end after it starts ({self.start} to {self.end})"
            )

    def check(self, frame: pd.DataFrame, *, available: str, start: str | None = None) -> None:
        """Raise `DiscoveryWindowError` if `frame` holds a row outside the window.

        Args:
            available: Column of the instants rows become available (tz-aware or UTC ns).
            start: Optional column of the instants rows start (checked against `start`).
        """
        late = _instants(frame[available]) > self.end.value
        if late.any():
            first = pd.Timestamp(int(_instants(frame[available])[late].min()), tz="UTC")
            raise DiscoveryWindowError(
                f"{int(late.sum())} row(s) become available after the discovery window ends at "
                f"{self.end} (first at {first}); exploratory research refuses post-discovery data"
            )
        if start is not None:
            early = _instants(frame[start]) < self.start.value
            if early.any():
                raise DiscoveryWindowError(
                    f"{int(early.sum())} row(s) start before the discovery window ({self.start})"
                )

    def until(self, end: TimestampLike) -> DiscoveryWindow:
        """The window cut at `end`; a later `end` is refused, never extended."""
        stop = ensure_utc(end)
        if stop > self.end:
            raise DiscoveryWindowError(
                f"{stop} is after the discovery window's end {self.end}; exploratory research "
                "refuses post-discovery data"
            )
        return DiscoveryWindow(self.start, stop, f"{self.rule}; cut at {stop}")

    def as_dict(self) -> dict[str, str]:
        return {"start": str(self.start), "end": str(self.end), "rule": self.rule}


def resolve_discovery_window(cfg: AppConfig, data_start: TimestampLike) -> DiscoveryWindow:
    """The discovery window of data starting at `data_start` (see the module docstring).

    Raises:
        DiscoveryWindowError: if the data starts at or after the vault, or after the window ends.
    """
    discovery = cfg.eda_config().discovery
    vault = vault_start(cfg)
    start = ensure_utc(data_start)
    if start >= vault:
        raise DiscoveryWindowError(f"the data starts at {start}, at or after vault.start {vault}")
    if discovery.end is not None:
        end = ensure_utc(discovery.end)
        rule = "eda.discovery.end (fixed)"
    else:
        cut = start + (vault - start) * discovery.fraction
        end = trading_day_bounds(trading_day(cut))[0]
        rule = (
            f"first {discovery.fraction:g} of the span from the data start to vault.start, "
            "at a trading-day start"
        )
    if end <= start:
        raise DiscoveryWindowError(
            f"the data starts at {start}, after the discovery window ends at {end}"
        )
    return DiscoveryWindow(start, end, rule)


# --- report builder ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Text:
    markdown: str


@dataclass(frozen=True)
class _Table:
    name: str
    frame: pd.DataFrame
    caption: str
    digits: int
    max_rows: int | None


@dataclass(frozen=True)
class _Figure:
    name: str
    png: bytes
    caption: str


@dataclass
class Section:
    """One Markdown document of a report; add elements in reading order."""

    key: str
    title: str
    elements: list[_Text | _Table | _Figure] = field(default_factory=list)

    def text(self, markdown: str) -> None:
        """A paragraph (or any Markdown block)."""
        self.elements.append(_Text(markdown.strip()))

    def table(
        self,
        name: str,
        frame: pd.DataFrame,
        *,
        caption: str,
        digits: int = 4,
        max_rows: int | None = None,
    ) -> None:
        """A table, saved in full as CSV and rendered with `digits` significant digits.

        With `max_rows`, the section shows at most that many rows (0: only the link to the CSV).
        """
        _check_key(name)
        if any(isinstance(e, _Table) and e.name == name for e in self.elements):
            raise ValueError(f"section {self.key!r} already has a table named {name!r}")
        if max_rows is not None and max_rows < 0:
            raise ValueError("max_rows must not be negative")
        self.elements.append(_Table(name, frame.copy(), caption, digits, max_rows))

    def figure(self, name: str, figure: Figure, *, caption: str) -> None:
        """A figure, rendered to PNG now (the figure object is not kept)."""
        _check_key(name)
        if any(isinstance(e, _Figure) and e.name == name for e in self.elements):
            raise ValueError(f"section {self.key!r} already has a figure named {name!r}")
        self.elements.append(_Figure(name, render_png(figure), caption))


class ReportBuilder:
    """Collects sections and writes a deterministic report directory (module docstring)."""

    def __init__(self, title: str, *, metadata: Mapping[str, Any]) -> None:
        self.title = title
        self.metadata = dict(metadata)
        self.sections: list[Section] = []
        self.attachments: dict[str, bytes] = {}

    def attach(self, name: str, content: bytes) -> None:
        """A further deterministic file at the report's top level (e.g. a generated YAML)."""
        reserved = {
            INDEX_FILE,
            METADATA_FILE,
            MANIFEST_FILE,
            *(f"{s.key}.md" for s in self.sections),
        }
        if "/" in name or name in reserved or name in self.attachments:
            raise ValueError(f"attachment name {name!r} is taken or not a plain file name")
        self.attachments[name] = content

    def section(self, key: str, title: str) -> Section:
        """A new section, written as ``<key>.md``."""
        _check_key(key)
        if key == Path(INDEX_FILE).stem or any(s.key == key for s in self.sections):
            raise ValueError(f"section key {key!r} is taken")
        section = Section(key, title)
        self.sections.append(section)
        return section

    def build(self, directory: Path) -> dict[str, str]:
        """Write the report into `directory` (created; must not hold a report yet).

        Returns:
            The SHA-256 of every written file, keyed by its path relative to `directory` (the
            content of ``manifest.json``, which is written last and not listed).
        """
        if (directory / MANIFEST_FILE).exists():
            raise FileExistsError(f"{directory} already holds a report")
        (directory / TABLES_DIR).mkdir(parents=True, exist_ok=True)
        (directory / FIGURES_DIR).mkdir(exist_ok=True)
        files: dict[str, bytes] = {
            METADATA_FILE: (json.dumps(self.metadata, indent=2, sort_keys=True) + "\n").encode(),
            **self.attachments,
        }
        for section in self.sections:
            lines = [f"# {section.title}", ""]
            for element in section.elements:
                lines.extend(_render(section.key, element, files))
                lines.append("")
            files[f"{section.key}.md"] = "\n".join(lines).rstrip("\n").encode() + b"\n"
        files[INDEX_FILE] = self._index().encode()
        digests: dict[str, str] = {}
        for relative in sorted(files):
            (directory / relative).write_bytes(files[relative])
            digests[relative] = hashlib.sha256(files[relative]).hexdigest()
        manifest = json.dumps({"files": digests}, indent=2, sort_keys=True) + "\n"
        (directory / MANIFEST_FILE).write_text(manifest, encoding="utf-8")
        return digests

    def _index(self) -> str:
        lines = [f"# {self.title}", "", "## Provenance", ""]
        for key in sorted(self.metadata):
            value = self.metadata[key]
            if isinstance(value, Mapping) and value:
                lines.append(f"- **{key}:**")
                lines.extend(f"  - {k}: {_inline(v)}" for k, v in sorted(value.items()))
            else:
                lines.append(f"- **{key}:** {_inline(value)}")
        lines.extend(["", "## Sections", ""])
        lines.extend(f"- [{s.title}]({s.key}.md)" for s in self.sections)
        return "\n".join(lines) + "\n"


def render_png(figure: Figure) -> bytes:
    """PNG bytes of `figure` without timestamps or software metadata (deterministic)."""
    FigureCanvasAgg(figure)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=FIGURE_DPI, metadata={"Software": None})
    return buffer.getvalue()


def new_figure(width: float = 8.0, height: float = 4.5) -> Figure:
    """A matplotlib figure detached from pyplot's global state."""
    return Figure(figsize=(width, height), layout="constrained")


def markdown_table(frame: pd.DataFrame, *, digits: int = 4) -> str:
    """A GitHub-flavoured Markdown table; floats with `digits` significant digits."""
    columns = [str(c) for c in frame.columns]
    rows = [[_cell(v, digits) for v in row] for row in frame.itertuples(index=False, name=None)]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in rows)
    return "\n".join(lines)


def _render(key: str, element: _Text | _Table | _Figure, files: dict[str, bytes]) -> list[str]:
    if isinstance(element, _Text):
        return [element.markdown]
    if isinstance(element, _Table):
        path = f"{TABLES_DIR}/{key}-{element.name}.csv"
        frame = element.frame
        files[path] = frame.to_csv(index=False, float_format="%.12g").encode()
        heading = f"**{element.caption}** ([csv]({path}))"
        shown = len(frame) if element.max_rows is None else min(len(frame), element.max_rows)
        if shown == 0 and len(frame):
            return [f"{heading}: {len(frame)} rows, in the CSV file only."]
        lines = [heading, "", markdown_table(frame.head(shown), digits=element.digits)]
        if shown < len(frame):
            lines.extend(["", f"*The first {shown} of {len(frame)} rows; all are in the CSV.*"])
        return lines
    path = f"{FIGURES_DIR}/{key}-{element.name}.png"
    files[path] = element.png
    return [f"![{element.caption}]({path})", "", f"*{element.caption}*"]


def _cell(value: Any, digits: int) -> str:
    if isinstance(value, bool | np.bool_):
        return "yes" if value else "no"
    if isinstance(value, float | np.floating):
        number = float(value)
        if math.isnan(number):
            return "n/a"
        if math.isinf(number):
            return "inf" if number > 0 else "-inf"
        return f"{number:.{digits}g}"
    if value is None or value is pd.NaT:
        return "n/a"
    return str(value).replace("|", "\\|")


def _inline(value: Any) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True, separators=(", ", ": "), default=str)
    if isinstance(value, Sequence) and not isinstance(value, str):
        return ", ".join(_inline(v) for v in value) if value else "none"
    return "none" if value is None else str(value)


def _check_key(key: str) -> None:
    if not key or any(c not in _KEY for c in key):
        raise ValueError(f"report keys use lowercase letters, digits, '_' and '-': {key!r}")


def _instants(column: pd.Series) -> np.ndarray[Any, np.dtype[np.int64]]:
    """UTC nanoseconds of a tz-aware or int64 column."""
    if pd.api.types.is_integer_dtype(column):
        return column.to_numpy(dtype=np.int64)
    values = pd.DatetimeIndex(column)
    if values.tz is None:
        raise NaiveTimestampError("instants must be tz-aware")
    return values.tz_convert("UTC").as_unit("ns").to_numpy(dtype="datetime64[ns]").view(np.int64)
