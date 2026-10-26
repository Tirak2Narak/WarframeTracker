"""Which build is running.

The number ("1.2") is the newest entry of wft/whatsnew.toml, the one file that
holds it. The commit says exactly which build it is. In a git checkout it is
read from git at runtime. A release tarball has no .git;
`git archive` fills `release_stamp.txt` (see .gitattributes) with the commit
and its date, and that is read instead. Neither is a second thing to keep in
step by hand.

Falls back to "unknown" rather than raising. Not knowing the version is a
nuisance; refusing to start over it would not be.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_STAMP = Path(__file__).resolve().parent / "release_stamp.txt"


def _in_checkout() -> bool:
    # Only ask git when this tree is itself a repository. Unpacked inside some
    # other repository, `git -C` would report that one's commit.
    return (_ROOT / ".git").exists()


def _stamp() -> tuple[str, str] | None:
    """(short commit, "YYYY-MM-DD HH:MM") from the archive stamp, if filled in."""
    try:
        lines = _STAMP.read_text().split("\n")
    except OSError:
        return None
    commit = lines[0].strip() if lines else ""
    if not commit or commit.startswith("$Format"):
        return None  # a checkout: git did not substitute it
    # "%ci" is "2026-10-14 12:34:56 +0200"; keep the date and minutes.
    stamp = lines[1].strip()[:16] if len(lines) > 1 else ""
    return commit, stamp or "unknown"


def number() -> str:
    """The release number, the newest entry of wft/whatsnew.toml ("unknown" if
    that file cannot be read)."""
    try:
        from wft import whatsnew
        return whatsnew.newest().version
    except Exception:  # noqa: BLE001 - a nuisance, not a reason not to start
        return "unknown"


@lru_cache(maxsize=1)
def describe() -> str:
    """Short commit, with a marker when the tree has uncommitted changes."""
    if not _in_checkout():
        stamped = _stamp()
        return stamped[0] if stamped else "unknown"
    try:
        commit = subprocess.run(
            ["git", "-C", str(_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if not commit:
            return "unknown"
        dirty = subprocess.run(
            ["git", "-C", str(_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return f"{commit}+local" if dirty else commit
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@lru_cache(maxsize=1)
def committed_at() -> str:
    """When that commit was made, so "is this current" has an answer."""
    if not _in_checkout():
        stamped = _stamp()
        return stamped[1] if stamped else "unknown"
    try:
        return subprocess.run(
            ["git", "-C", str(_ROOT), "log", "-1", "--format=%cd",
             "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
