"""Where the tests that need the game's data get it from, without touching the real database.

The game data (items, parts, relics) is what `bin/wf update` downloads. A test
that needs it works on a copy made through a read-only connection; a plain
`db.connect` on the real file would create or migrate it, which is a write.
`WFT_TEST_DATABASE` names the database to copy from, otherwise the configured
one is used. Without game data in it, the test says so (`SKIPPED: ...`).
"""
from __future__ import annotations

import os
import pathlib
import sqlite3

#: What a skipped test tells a reader about getting the data.
HOW_TO_GET_THE_DATA = (
    'Run "bin/wf update" once (it downloads Digital Extremes\' public export), '
    "or set WFT_TEST_DATABASE to a database that has it."
)


def source_database(config_mod) -> pathlib.Path:
    """`WFT_TEST_DATABASE`, else the configured database."""
    chosen = os.environ.get("WFT_TEST_DATABASE")
    return pathlib.Path(chosen) if chosen else config_mod.load().db_path


def has_game_data(path: pathlib.Path) -> bool:
    """True when the database holds the game's parts. Opened read-only: never creates or migrates."""
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return connection.execute("SELECT COUNT(*) FROM parts").fetchone()[0] > 0
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def copy_database(source: pathlib.Path, target: pathlib.Path) -> None:
    """A copy made through a read-only connection, so the source is never written."""
    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        live.backup(copy)
        copy.close()
    finally:
        live.close()
