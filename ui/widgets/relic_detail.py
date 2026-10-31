"""What one relic drops, and which of it is worth having.

The set window answers "what is missing and which relics drop it" and stops at
the relic's name. The next question is the one a player asks out loud: fine,
and what else is in that relic? A relic is opened whole - the four rewards on
the screen are drawn from four relics, not four pieces of one - so what else is
inside decides whether opening it is worth a run at all.

`fill` rather than a dialog method, because three screens ask this: the window
opened from a set, the same window opened from the plan's openings table, and
the Collection page's relic tab, which had a smaller version of its own. That
version listed the rewards and their rarity and never said which refinement the
relic was - the one fact that decides every drop chance on the page.

The relic is addressed by name rather than by identifier. A name covers four
refinements that drop exactly the same six things at different odds, so the
name is what a person means by "Lith O4" - and the odds shown are the ones for
the refinement actually on the shelf.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QLabel, QTableWidgetItem, QVBoxLayout,
)

from ui import theme
from ui.widgets.dialog_size import fit_dialog
from ui.widgets.tables import simple_table
from wft.data import db as dbq

#: Rarities in the order the reward screen thinks of them, best last.
_ORDER = {"COMMON": 0, "UNCOMMON": 1, "RARE": 2}


def held_by_refinement(connection, relic_name: str) -> dict[str, int]:
    """{refinement: count} for one printed name, empty refinements left out.

    The shelf is kept per refinement because the refinement is part of the
    identifier, and it has to be shown that way too: twelve Intact and three
    Radiant are not fifteen of one thing, they are two different sets of odds.
    """
    out: dict[str, int] = {}
    for row in connection.execute(
            "SELECT r.unique_name, o.count FROM relics r "
            "JOIN owned_relics o ON o.relic_unique = r.unique_name "
            "WHERE r.name = ? AND o.count > 0", (relic_name,)):
        refinement = dbq.refinement_of(row["unique_name"])
        out[refinement] = out.get(refinement, 0) + int(row["count"])
    return out


def _standing(connection, reward, retained=None) -> QTableWidgetItem:
    """Where this reward stands against what the user already holds."""
    part = reward["part_unique"]
    if part is None or reward["name"] is None:
        return QTableWidgetItem("—")
    if (reward["category"] or "") in dbq.NOT_FOR_MASTERY_CATEGORIES:
        # Forma and its kind. Not a gap in a collection, so calling it
        # "needed" would mark every relic in the game as worth opening.
        item = QTableWidgetItem("not tracked")
        item.setForeground(QColor(theme.TEXT_FAINT))
        return item

    # The plan's own rule, not a second one written here. "Built" is not the
    # end of it: a built Bronco Prime is still owed to Akbronco Prime, and
    # this column said "built" against a receiver the user has to go and find.
    from wft.relic import openings

    held = dbq.owned_count(connection, part)
    need = max(1, int(reward["count_needed"] or 1),
               int((retained or {}).get(dbq.canonical(connection, part), 0)))
    if openings.needed(connection, part, retained):
        item = QTableWidgetItem(f"NEED  {held} of {need}")
        item.setForeground(QColor(theme.GOOD))
        return item
    if reward["item_unique"] and dbq.item_built(connection, reward["item_unique"]):
        item = QTableWidgetItem("built")
        item.setForeground(QColor(theme.TEXT_DIM))
        return item
    item = QTableWidgetItem(f"have {held}" + (f" of {need}" if need > 1 else ""))
    item.setForeground(QColor(theme.TEXT_DIM))
    return item


def fill(layout, connection, relic_name: str, with_heading: bool = True):
    """Build one relic's detail into `layout`; returns its rewards table."""
    if with_heading:
        heading = QLabel(relic_name)
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)

    # Every refinement of this name. This is also how the right `unique_name`
    # is found without the caller having to know which of the four it meant.
    stock = connection.execute(
        "SELECT unique_name, vaulted FROM relics WHERE name = ?",
        (relic_name,)).fetchall()
    if not stock:
        layout.addWidget(QLabel("This relic is not in the catalogue."))
        return

    by_refinement = {dbq.refinement_of(row["unique_name"]): row for row in stock}
    on_shelf = held_by_refinement(connection, relic_name)
    # The refinement to quote odds for: the one held, and the one held most of
    # when several are. Someone with thirteen Intact and one Radiant is asking
    # about the thirteen.
    refinement = (max(on_shelf.items(), key=lambda pair: pair[1])[0]
                  if on_shelf else "INTACT")
    if refinement not in by_refinement:
        refinement = next(iter(by_refinement))
    total_held = sum(on_shelf.values())

    vaulted = next((row["vaulted"] for row in stock
                    if row["vaulted"] is not None), None)
    state = [
        "vault status unknown" if vaulted is None else
        ("vaulted — no more of these will drop" if vaulted else
         "available — still drops"),
        f"{total_held} held" if total_held else "none held",
        f"odds shown for {refinement.title()}",
    ]
    summary = QLabel("  ·  ".join(state))
    summary.setObjectName("ViewSubtitle")
    summary.setWordWrap(True)
    layout.addWidget(summary)

    if on_shelf:
        spread = ", ".join(f"{name.title()} ×{count}"
                           for name, count in sorted(on_shelf.items()))
        held_line = QLabel(f"On the shelf: {spread}")
        held_line.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(held_line)

    chances = dbq.DROP_CHANCE.get(refinement, dbq.DROP_CHANCE["INTACT"])
    rewards = connection.execute(
        "SELECT rr.rarity, rr.part_unique, p.name, p.ducats, "
        "       p.count_needed, p.item_unique, i.name AS item_name, i.category "
        "FROM relic_rewards rr "
        "LEFT JOIN parts p ON p.unique_name = rr.part_unique "
        "LEFT JOIN items i ON i.unique_name = p.item_unique "
        "WHERE rr.relic_unique = ?",
        (by_refinement[refinement]["unique_name"],)).fetchall()

    rows = sorted(
        ((_ORDER.get((r["rarity"] or "COMMON").upper(), 0), r["name"] or "", r)
         for r in rewards),
        key=lambda entry: (-entry[0], entry[1]))

    # The shared table. Always six rows, so it is fitted: the dialog
    # and the Collection pane both put it where the page scrolls. The reward
    # name is the column that wraps; it was squeezed to 81 of 425px at 20pt.
    # One walk of the recipe graph for the six rows.
    retained = dbq.retained_counts(connection)
    table = simple_table(["Reward", "Belongs to", "Rarity", "Chance", "Ducats",
                          "You"], len(rows), stretch=0, wrap=True)
    for index, (_order, _name, reward) in enumerate(rows):
        rarity = (reward["rarity"] or "COMMON").upper()
        table.setItem(index, 0, QTableWidgetItem(reward["name"] or "(unresolved)"))
        table.setItem(index, 1, QTableWidgetItem(reward["item_name"] or "—"))

        mark = QTableWidgetItem(rarity.title())
        mark.setForeground(QColor(
            theme.ACCENT if rarity == "RARE" else
            (theme.TEXT if rarity == "UNCOMMON" else theme.TEXT_DIM)))
        table.setItem(index, 2, mark)

        table.setItem(index, 3, QTableWidgetItem(f"{chances.get(rarity, 0.0):.1%}"))
        table.setItem(index, 4, QTableWidgetItem(
            str(reward["ducats"]) if reward["ducats"] else "—"))

        # The column this exists for. "Have it" and "need it" are the
        # difference between a run worth doing and a run for ducats, and they
        # cannot be read off the reward name.
        table.setItem(index, 5, _standing(connection, reward, retained))

    layout.addWidget(table)
    return table


class RelicDetail(QDialog):
    """One relic: what it drops, at what odds, and what you still need."""

    def __init__(self, connection, relic_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(relic_name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.7))
        table = fill(layout, connection, relic_name)
        layout.addStretch(1)
        fit_dialog(self, wide=(table,) if table is not None else ())
