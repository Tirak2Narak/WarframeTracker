"""How fresh the local reference data is, for the Settings page and `wf doctor`.

One reading of the `meta` table, so the two cannot disagree about whether the
database is built or how old it is. The Settings page used to carry this
inline, which a command-line diagnosis could not call without Qt.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wft.config import DB_NAME, Config
from wft.data import db as dbq


@dataclass
class DataStatus:
    readable: bool = False                  # the database opened and has a meta table
    last_update: datetime | None = None
    counts: dict = field(default_factory=dict)
    counts_text_ok: bool = True            # False when the counts row is not JSON
    has_counts_row: bool = False
    vault_age: int | None = None

    @property
    def age_days(self) -> int | None:
        if self.last_update is None:
            return None
        return (datetime.now(timezone.utc) - self.last_update).days


def read(cfg: Config, *, read_only: bool = False) -> DataStatus:
    """Read the status. `read_only` never creates or alters any file.

    The normal path goes through `db.connect`, which creates the file and the
    schema if they are missing - right for the application, wrong for a
    diagnosis, which must leave the machine as it found it.
    """
    status = DataStatus()
    try:
        if read_only:
            path = cfg.data_dir_path() / DB_NAME
            if not path.is_file():
                return status
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        else:
            connection = dbq.connect(cfg.db_path)
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='last_update_at'").fetchone()
            counts_row = connection.execute(
                "SELECT value FROM meta WHERE key='counts'").fetchone()
            status.vault_age = dbq.vault_data_age(connection)
        finally:
            connection.close()
    except Exception:  # noqa: BLE001
        # No data, or what is there cannot be read: callers say so and offer
        # the update. Failing here would take the page that fixes it down.
        return status

    status.readable = True
    if row:
        try:
            status.last_update = datetime.fromisoformat(row["value"])
        except ValueError:
            pass
    if status.last_update is None:
        # Fall back to the manifest hash file's mtime - a data set built by
        # `wf update` before the GUI recorded its own timestamp.
        manifests = (cfg.data_dir_path() / "manifests") if read_only else cfg.manifest_dir
        hashes = manifests / "index-hashes.json"
        if hashes.is_file():
            status.last_update = datetime.fromtimestamp(
                hashes.stat().st_mtime, tz=timezone.utc)

    if counts_row:
        status.has_counts_row = True
        try:
            status.counts = json.loads(counts_row["value"])
        except json.JSONDecodeError:
            status.counts_text_ok = False
    return status
