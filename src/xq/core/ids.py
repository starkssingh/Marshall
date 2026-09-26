"""Identifiers for runs and code versions (ARCH-005)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ulid import ULID

UNKNOWN_SHA = "unknown"


def new_ulid() -> str:
    """Return a new ULID string: 26 characters, lexicographically sortable by creation time."""
    return str(ULID())


def git_sha(repo: Path | None = None) -> str:
    """Return the commit hash checked out in `repo` (default: current directory).

    Returns `UNKNOWN_SHA` when git is unavailable or the directory is not a repository, for example
    inside a container built without ``.git``. Confirmatory research runs refuse that value.
    """
    output = _git(repo, "rev-parse", "HEAD")
    return output.strip() if output else UNKNOWN_SHA


def git_is_dirty(repo: Path | None = None) -> bool | None:
    """Return True if tracked or untracked files differ from HEAD, or None if unknown."""
    output = _git(repo, "status", "--porcelain")
    if output is None:
        return None
    return output.strip() != ""


def _git(repo: Path | None, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout
