"""Writing the inventory out, and reading it back.

The holdings are the only thing here that cannot be regenerated: the
manifests reload in three seconds, but a recording takes minutes to make and
minutes to read, and a hand-entered correction is gone for good, so they need a
way to be saved.

The file carries names beside identifiers. The identifiers are what restore
uses — they are stable and unambiguous — but a backup nobody can read is a
backup nobody checks, and a name is what tells you whether the file you are
about to restore is the right one.

Mastery travels with it. It is a latch that no scan clears, so losing it means
losing the record of everything sold, which is the part that cannot be
re-scanned at all.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

#: Bumped when the shape changes in a way an older reader would misread.
FORMAT = 1


def export_holdings(connection: sqlite3.Connection) -> dict:
    """Everything the user put in, in a form that can be read back."""
    parts = [dict(row) for row in connection.execute(
        "SELECT o.part_unique, p.name, o.count, o.source, o.updated_at "
        "FROM owned_parts o LEFT JOIN parts p ON p.unique_name = o.part_unique "
        "ORDER BY p.name")]
    items = [dict(row) for row in connection.execute(
        "SELECT o.item_unique, i.name, o.count, o.mastered, o.source, o.updated_at "
        "FROM owned_items o LEFT JOIN items i ON i.unique_name = o.item_unique "
        "ORDER BY i.name")]
    relics = [dict(row) for row in connection.execute(
        "SELECT o.relic_unique, r.name, o.count, o.source, o.updated_at "
        "FROM owned_relics o LEFT JOIN relics r ON r.unique_name = o.relic_unique "
        "ORDER BY r.name")]
    # The dismissed readings are user decisions too, and re-answering forty of
    # them is the same tedium as the review they came from.
    ignores = [row["reading"] for row in connection.execute(
        "SELECT reading FROM learned_ignores ORDER BY reading")]

    # The copies a player keeps for themselves: a decision of theirs, like
    # the above.
    kept = [row["item_unique"] for row in connection.execute(
        "SELECT item_unique FROM kept_items ORDER BY item_unique")]

    return {
        "format": FORMAT,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {"parts": len(parts), "items": len(items),
                   "relics": len(relics), "dismissed": len(ignores),
                   "kept": len(kept)},
        "parts": parts,
        "items": items,
        "relics": relics,
        "dismissed": ignores,
        "kept": kept,
    }


def write(connection: sqlite3.Connection, path: str) -> dict:
    data = export_holdings(connection)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False)
    return data["counts"]


def read(path: str) -> dict:
    """Load a backup, or raise with something a person can act on."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ValueError(f"Could not read that file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("That file is not a backup written by this tool.") from exc

    if not isinstance(data, dict) or "parts" not in data or "items" not in data:
        raise ValueError("That JSON is not an inventory backup.")
    if int(data.get("format", 0)) > FORMAT:
        raise ValueError(
            f"That backup is format {data.get('format')} and this build reads "
            f"{FORMAT}. Update the tool rather than restoring it half-read.")
    return data


def restore(connection: sqlite3.Connection, data: dict, replace: bool = True) -> dict:
    """Write a backup back into the database.

    `replace` empties the holdings first, which is what restoring a backup
    normally means. Without it the file is merged in, and merging counts would
    be a guess — so a row already present keeps the higher count, and mastery
    is or-ed, since a latch that either side set is set. The marks of kept
    copies follow the same split: replaced with the file when
    `replace`, added to otherwise. A backup from before them has no `kept`
    key; restoring it with `replace` clears the marks like every other table.
    """
    written = {"parts": 0, "items": 0, "relics": 0, "dismissed": 0, "kept": 0}
    if replace:
        for table in ("owned_parts", "owned_items", "owned_relics",
                      "learned_ignores", "kept_items"):
            connection.execute(f"DELETE FROM {table}")

    from wft.data.db import is_uncounted

    # An older backup can carry a count of Forma; it stays out.
    for row in data.get("parts", []):
        if is_uncounted(connection, row["part_unique"]):
            continue
        connection.execute(
            "INSERT INTO owned_parts(part_unique, count, source, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(part_unique) DO UPDATE SET "
            "count = MAX(count, excluded.count), source = excluded.source",
            (row["part_unique"], int(row.get("count") or 0),
             row.get("source") or "restored", row.get("updated_at")))
        written["parts"] += 1

    for row in data.get("items", []):
        if is_uncounted(connection, row["item_unique"]):
            continue
        connection.execute(
            "INSERT INTO owned_items(item_unique, count, mastered, source, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(item_unique) DO UPDATE SET "
            "count = MAX(count, excluded.count), "
            "mastered = MAX(mastered, excluded.mastered), "
            "source = excluded.source",
            (row["item_unique"], int(row.get("count") or 0),
             int(row.get("mastered") or 0), row.get("source") or "restored",
             row.get("updated_at")))
        written["items"] += 1

    for row in data.get("relics", []):
        connection.execute(
            "INSERT INTO owned_relics(relic_unique, count, source, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(relic_unique) DO UPDATE SET "
            "count = MAX(count, excluded.count), source = excluded.source",
            (row["relic_unique"], int(row.get("count") or 0),
             row.get("source") or "restored", row.get("updated_at")))
        written["relics"] += 1

    for reading in data.get("dismissed", []):
        connection.execute(
            "INSERT OR IGNORE INTO learned_ignores(reading) VALUES(?)", (reading,))
        written["dismissed"] += 1

    for unique in data.get("kept", []):
        connection.execute(
            "INSERT OR IGNORE INTO kept_items(item_unique, updated_at) "
            "VALUES(?, datetime('now'))", (unique,))
        written["kept"] += 1

    connection.commit()
    return written
