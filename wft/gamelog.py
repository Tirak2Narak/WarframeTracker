"""Finding Warframe's EE.log without asking the user for a path.

The game writes the log inside its Proton prefix, and where that prefix lives
depends on how Steam was installed and on which library the game sits in: the
normal Steam folder, the Flatpak's own, or a second drive that `libraryfolders.vdf`
names. A newcomer cannot be expected to know any of that, and a wrong
`paths.ee_log` fails quietly (the tool then matches rewards against every prime
part instead of the relics in play).

This only reads two kinds of local file, `libraryfolders.vdf` and a stat of the
candidate log. It never opens the log (it holds an IP address and an e-mail
address; see `wf doctor`) and it makes no network request. No Qt: `wf doctor`
and the Setup page ask the same function, so they name the same file.

Where Steam may be and where inside a library the log sits are config values
(`paths.steam_roots`, `paths.ee_log_in_library`), not constants here.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

#: `"path"  "/mnt/games/SteamLibrary"` in Valve's text format. A backslash in the
#: value is escaped as two, and a quote as backslash-quote.
_PATH_LINE = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')

#: Where the file that lists the libraries sits, relative to a Steam folder.
#: Current Steam keeps it under steamapps; older installs under config.
_LIBRARY_LISTS = ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf")


def libraries(steam_root: Path) -> list[Path]:
    """The Steam folder itself plus every library `libraryfolders.vdf` lists."""
    found = [steam_root]
    for relative in _LIBRARY_LISTS:
        try:
            text = (steam_root / relative).read_text(errors="replace")
        except OSError:
            continue
        for match in _PATH_LINE.finditer(text):
            found.append(Path(re.sub(r"\\(.)", r"\1", match.group(1))))
    return found


def find(roots: list[str], relative: str) -> list[Path]:
    """Every existing EE.log under the given Steam folders, newest first.

    `roots` are Steam folders as written in the config (`~` allowed). The same
    library is reached more than once (`~/.steam/steam` is a link to
    `~/.local/share/Steam`), so results are de-duplicated by real path.
    """
    seen: dict[str, Path] = {}
    for root in roots:
        root_path = Path(os.path.expanduser(str(root)))
        if not root_path.is_dir():
            continue
        for library in libraries(root_path):
            candidate = library / relative
            try:
                if not candidate.is_file():
                    continue
                real = str(candidate.resolve())
            except OSError:
                continue
            seen.setdefault(real, candidate)

    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(seen.values(), key=modified, reverse=True)


def find_for(cfg) -> list[Path]:
    """`find` with the Steam folders and the in-library path from the config."""
    roots = cfg.get("paths.steam_roots", []) or []
    relative = str(cfg.get("paths.ee_log_in_library", "") or "")
    if not roots or not relative:
        return []
    return find(list(roots), relative)
