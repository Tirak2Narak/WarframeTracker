"""The list of releases (wft/whatsnew.toml) and what can be said about it.

The file is the only place a version number is written down. Everything else
asks here: the running version is its newest entry, the "What's new" window
shows its entries, and the release script compares it with PATCHNOTES.md.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent / "whatsnew.toml"
_NUMBER = re.compile(r"^\d+(\.\d+)*$")


@dataclass(frozen=True)
class Release:
    version: str
    date: str          # "YYYY-MM-DD", or "" while not shipped
    lines: tuple[str, ...]


def key(version: str) -> tuple[int, ...]:
    """A version as numbers, so that 1.10 is newer than 1.9."""
    text = str(version).strip()
    if not _NUMBER.match(text):
        raise ValueError(f"not a version number: {version!r}")
    return tuple(int(part) for part in text.split("."))


def load(path: Path | str | None = None) -> list[Release]:
    """Every release, newest first. Raises on a file that is not usable."""
    source = Path(path) if path else DEFAULT_FILE
    with open(source, "rb") as handle:
        data = tomllib.load(handle)
    found = []
    for row in data.get("release", []):
        lines = tuple(str(line) for line in row.get("lines", []))
        found.append(Release(str(row["version"]), str(row.get("date", "")), lines))
    if not found:
        raise ValueError(f"{source}: no [[release]] entry")
    found.sort(key=lambda release: key(release.version), reverse=True)
    return found


@lru_cache(maxsize=1)
def releases() -> tuple[Release, ...]:
    return tuple(load())


def newest(path: Path | str | None = None) -> Release:
    return load(path)[0] if path else releases()[0]


def is_newer(version: str, than: str) -> bool:
    return key(version) > key(than)


def unseen(seen: str, path: Path | str | None = None) -> list[Release]:
    """The releases newer than `seen` ("" = none seen: all of them)."""
    every = load(path) if path else list(releases())
    if not seen:
        return every
    try:
        seen_key = key(seen)
    except ValueError:
        return every
    return [r for r in every if key(r.version) > seen_key]


# -- the release check (packaging/make-release.sh) ----------------------------

_HEADING = re.compile(r"^##\s+(\d+(?:\.\d+)*)(?:\s+[—–-]\s+(\d{4}-\d{2}-\d{2}))?\s*$")


def check_release(whatsnew_file: Path | str, patchnotes_file: Path | str) -> list[str]:
    """What stops a release: an empty list means the two files agree.

    The newest entry of whatsnew.toml needs a date, and the newest heading of
    PATCHNOTES.md ("## 1.2 — 2026-10-21") must carry the same number and date.
    """
    problems = []
    try:
        top = load(whatsnew_file)[0]
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        return [f"{whatsnew_file}: cannot be read ({exc})"]
    if not top.date:
        problems.append(f"whatsnew.toml: version {top.version} has no date")
    heading = None
    for line in Path(patchnotes_file).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = _HEADING.match(line)
            if heading is None:
                return [f"PATCHNOTES.md: newest heading is not '## <number> — "
                        f"<YYYY-MM-DD>': {line!r}"]
            break
    if heading is None:
        return ["PATCHNOTES.md: no '## ' heading"]
    number, date = heading.group(1), heading.group(2) or ""
    if number != top.version:
        problems.append(f"version differs: whatsnew.toml says {top.version}, "
                        f"PATCHNOTES.md says {number}")
    if not date:
        problems.append("PATCHNOTES.md: the newest heading has no date")
    elif top.date and date != top.date:
        problems.append(f"date differs: whatsnew.toml says {top.date}, "
                        f"PATCHNOTES.md says {date}")
    return problems


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python3 -m wft.whatsnew WHATSNEW.toml PATCHNOTES.md", file=sys.stderr)
        return 2
    problems = check_release(args[0], args[1])
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
