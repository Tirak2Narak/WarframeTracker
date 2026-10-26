"""The two ways back to a clean start, behind the Reset card in Settings.

"Delete my inventory" empties the user's own tables and leaves the settings and
the game data alone; "Delete everything" takes every file the tracker wrote and
starts the program again, as on its first launch.

Files, not folders: the data folder may be shared. Other tools can keep their
own files in it, and the program folder knows only the names it
writes itself, so a folder is removed only when nothing else is left in it. The
same goes for the config folder. Captures and recordings in the user's own
folders are theirs and are never touched.

Everything goes to the trash where the desktop has one, so a mistaken click can
be taken back from the file manager; only a file that cannot be moved there is
deleted outright.
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QFile

from ui import geometry, settings_io, theme
from wft import logfile


def tracker_files(cfg) -> list[Path]:
    """Every file and folder the tracker itself writes, that exists now.

    Each path comes from the code that writes it, so a file that moves cannot
    be missed here: the user config from `settings_io`, the window sizes from
    `geometry`, the glyph cache from `theme`, the rest from `cfg`.
    """
    data = cfg.data_dir_path()
    database = data / cfg.db_path.name
    candidates = [
        settings_io.target_config_path(),
        Path(geometry.settings_file()),
        database,
        # SQLite's own companions, present after a crash or in WAL mode.
        *(database.with_name(database.name + suffix)
          for suffix in ("-journal", "-wal", "-shm")),
        data / cfg.droptables_path.name,
        data / "manifests",
        data / "trading",
        logfile.log_dir(cfg),
        Path(theme.cache_dir()),
    ]
    seen: list[Path] = []
    for path in candidates:
        if (path.exists() or path.is_symlink()) and path not in seen:
            seen.append(path)
    return seen


def folders_to_tidy(cfg) -> list[Path]:
    """Folders that are removed after a full reset, but only when empty."""
    return [cfg.data_dir_path(), settings_io.target_config_path().parent]


def to_trash(path: Path) -> bool:
    """Move one file or folder to the desktop's trash; False when it cannot."""
    result = QFile.moveToTrash(str(path))
    # PySide6 returns the bool alone or (bool, path in trash) by version.
    return bool(result[0] if isinstance(result, tuple) else result)


def discard(path: Path, trash=None) -> str:
    """'trash' or 'deleted': the trash where possible, deleted where not."""
    if (trash or to_trash)(path):
        return "trash"
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    return "deleted"


def delete_everything(cfg, trash=None) -> dict[str, list[Path]]:
    """Discard every tracker file; returns {'trash': [...], 'deleted': [...]}."""
    outcome: dict[str, list[Path]] = {"trash": [], "deleted": []}
    for path in tracker_files(cfg):
        outcome[discard(path, trash)].append(path)
    for folder in folders_to_tidy(cfg):
        try:
            folder.rmdir()
        except OSError:
            # Not empty (another tool's files, an older backup) or gone already.
            pass
    return outcome


def backup_before_deleting(cfg, connection,
                           trash=None) -> tuple[Path, bool] | None:
    """A backup of the holdings, put in the trash: (file, in the trash), or None.

    The backup is the one "Restore from a backup..." on the Inventory page
    reads, so the way back from a mistaken click is a button the user already
    has. None when there was nothing to back up; when the trash refuses the
    file, it stays in the data folder and the flag says so.
    """
    from wft.data import backup

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = cfg.data_dir_path() / f"inventory-before-delete-{stamp}.json"
    counts = backup.write(connection, str(path))
    if not any(counts.values()):
        path.unlink()
        return None
    return path, (trash or to_trash)(path)


def restart() -> None:
    """Start the program again in this process, as on its first launch.

    An exec rather than a second process and a quit: nothing of the old one
    runs on, so nothing can write a file back after it was deleted (the window
    stores its size when it closes), and there is never a moment with two
    trackers listening for the same hotkey. Without the old arguments: a
    `--page` from the menu's quick actions would open that page instead of
    Setup, which is where a start without game data belongs.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, sys.argv[0]])
