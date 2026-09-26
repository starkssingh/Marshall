"""ARCH-005: run and code identifiers."""

import re
import subprocess
from pathlib import Path

from xq.core.ids import UNKNOWN_SHA, git_is_dirty, git_sha, new_ulid

CROCKFORD_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_ulids_are_well_formed_and_unique() -> None:
    ids = [new_ulid() for _ in range(1000)]
    assert all(CROCKFORD_ULID.match(value) for value in ids)
    assert len(set(ids)) == len(ids)


def test_git_sha_and_dirty_state(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "file.txt").write_text("one")
    git("add", "file.txt")
    git("commit", "-q", "-m", "initial")

    sha = git_sha(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert git_is_dirty(tmp_path) is False

    (tmp_path / "file.txt").write_text("two")
    assert git_is_dirty(tmp_path) is True


def test_git_sha_outside_a_repository(tmp_path: Path) -> None:
    assert git_sha(tmp_path) == UNKNOWN_SHA
    assert git_is_dirty(tmp_path) is None
