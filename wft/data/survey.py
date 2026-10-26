"""Every tracked set with what is held and what is missing.

One pass over the sets rather than one query per section: each section of the
Plan page, and each Trading list, is a different question about the same rows,
and `set_progress` alone is already a query per item. It lived on the Plan page
and moved here unchanged so the Trading lists ask the same question the same way.
"""
from __future__ import annotations

import sqlite3

from wft.data import db as dbq


def set_survey(conn: sqlite3.Connection, include_unstarted: bool = False) -> list[dict]:
    """Every tracked set, with what is held and what is missing.

    Untouched sets (nothing held, nothing built) are left out unless
    `include_unstarted`: on the Plan page they are the rest of the game, but a
    list of sets missing at most 3 or 4 pieces has to see them.
    """
    rows = conn.execute(
        "SELECT i.unique_name, i.name, i.category FROM items i "
        "WHERE i.unique_name IN (SELECT DISTINCT item_unique FROM parts) "
        "AND i.category NOT IN ('reward_other', 'component_only') "
        "ORDER BY i.name").fetchall()

    survey = []
    for row in rows:
        held, total = dbq.set_progress(conn, row["unique_name"])
        if not total:
            continue
        built = dbq.item_built(conn, row["unique_name"])
        if held == 0 and not built and not include_unstarted:
            # Untouched sets are not a plan, they are the rest of the game.
            continue

        missing = []
        # A built item owes nothing. Its pieces were consumed making it -
        # that is what building means - so the parts table reads 0 held and
        # every one of them looked outstanding. A player with Grattler in
        # the arsenal at rank 4 was asked by the plan to farm its
        # blueprint; 97 built items were each doing this, which is most of
        # what made "pieces you still need" a list of 328.
        #
        # Deliberately not "unless a second one is wanted": a second copy
        # earns no mastery and this page is a shopping list, not a ledger.
        for part in ([] if built else conn.execute(
                "SELECT unique_name, name, count_needed FROM parts "
                "WHERE item_unique=? AND alias_of IS NULL", (row["unique_name"],))):
            short = part["count_needed"] - dbq.crafting_stock(conn, part["unique_name"])
            if short > 0:
                vaulted, _share = dbq.vault_state(conn, part["unique_name"])
                missing.append((part["name"], short, vaulted))

        survey.append({
            "name": row["name"], "category": row["category"],
            "held": held, "total": total, "built": built, "missing": missing,
            "unique": row["unique_name"],
            "mastered": dbq.is_mastered(conn, row["unique_name"]),
        })
    return survey
