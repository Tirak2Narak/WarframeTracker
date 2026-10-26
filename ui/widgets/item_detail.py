"""What a set still needs, and how to get it.

The progress table answers "how far along am I" and stops there. The next
question is always the same and had no answer anywhere in the tool: which
piece is missing, what drops it, and do I already hold one of those relics.
The data was there - the parts table, the relic rewards, the vault status, the
relic inventory - and nothing put it in one place.

A dialog rather than an expanding row. A row that unfolds inside a sorted
table has to survive re-sorting and re-rendering, and the content here is a
table of its own. Opening it on double-click keeps the list unchanged for
people who only want the overview.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ui import theme
from ui.widgets.dialog_size import fit_dialog
from ui.widgets.tables import simple_table
from wft.data import db as dbq


def state_line(connection, item_unique: str) -> str:
    """What is held, what is built and whether the mastery is banked.

    One sentence with one author. It was said in two places - this dialog and
    the Collection detail pane - and the two said it differently: the pane
    named the pieces and the copies and left the mastery out altogether, which
    is the fact the whole shelf rule turns on and the one nobody could
    find anywhere on the page.
    """
    held, total = dbq.set_progress(connection, item_unique)
    built = dbq.item_built(connection, item_unique)

    state = []
    if total:
        state.append(f"{held} of {total} pieces held")
    if built:
        state.append(f"built x{built}")
    state.append("mastered" if dbq.is_mastered(connection, item_unique)
                 else "not mastered")
    return "  ·  ".join(state)


def held_text(connection, piece_unique: str) -> tuple[int, str]:
    """(copies there for the recipe, what the Held cell says).

    The number is `crafting_stock`; the text adds the copy the user keeps, so
    "3 (1 kept)" is three built and two for crafting. With none built yet the
    kept copy is still to come, not held, and the cell must not say -1.
    """
    stock = dbq.crafting_stock(connection, piece_unique)
    if not dbq.is_kept(connection, piece_unique):
        return stock, str(stock)
    return stock, (f"{stock + 1} (1 kept)" if stock >= 0 else "0 (1 to keep)")


def add_keep_box(connection, layout, item_unique: str, item_name: str,
                 changed) -> None:
    """The "keep one for myself" mark, for a piece of another recipe.

    Only an item something is built from has the question: Lex Prime is
    two of Aklex Prime, a Braton is nothing's ingredient. Written at once,
    like the other single-field controls, so there is nothing to cancel.
    `changed` is called after the write, for the page to redraw what the
    mark moves. One function for the item dialog and the Collection's Items
    pane, so the two never ask differently.
    """
    parents = dbq._consumers(connection).get(item_unique)
    if not parents:
        return
    marks = ",".join("?" * len(parents))
    names = [row["name"] for row in connection.execute(
        f"SELECT DISTINCT name FROM items WHERE unique_name IN ({marks}) "
        f"ORDER BY name", tuple(parents))]
    parent_text = " and ".join(names) if names else "the items built from it"

    box = QCheckBox(f"Keep one {item_name} for myself, not for crafting "
                    f"{parent_text}")
    box.setChecked(dbq.is_kept(connection, item_unique))

    def write(checked: bool) -> None:
        dbq.set_kept(connection, item_unique, bool(checked))
        changed()

    box.toggled.connect(write)
    layout.addWidget(box)
    why = QLabel(
        f"With the box ticked the tool counts one {item_name} more than "
        f"{parent_text} needs, and holds that many sets of pieces back.")
    why.setObjectName("ViewSubtitle")
    why.setWordWrap(True)
    layout.addWidget(why)


class ItemDetail(QDialog):
    """One set: its pieces, and the relics that drop the missing ones."""

    def __init__(self, connection, item_unique: str, item_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(item_name)
        # Set when the "keep one" box was ticked or cleared, so the page that
        # opened this dialog knows to redraw what the mark moves.
        self.kept_changed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.7))

        heading = QLabel(item_name)
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)

        summary = QLabel(state_line(connection, item_unique))
        summary.setObjectName("ViewSubtitle")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        add_keep_box(connection, layout, item_unique, item_name,
                     lambda: setattr(self, "kept_changed", True))

        parts = connection.execute(
            "SELECT unique_name, name, count_needed FROM parts "
            "WHERE item_unique = ? AND alias_of IS NULL ORDER BY name",
            (item_unique,)).fetchall()

        # Rows, not parts: a piece that is itself built gets a second line
        # under it naming what building it would take. Akbronco Prime wants
        # two Bronco Primes, and "Bronco Prime 1 of 2" stops exactly where the
        # question starts - a Bronco Prime does not drop, its blueprint and
        # its receiver do.
        #
        # Held is what is there for this recipe (`crafting_stock`): a copy the
        # user keeps for themselves is shown beside the count and not in it.
        entries = []
        missing = []
        for part in parts:
            owned = dbq.crafting_stock(connection, part["unique_name"])
            need = int(part["count_needed"])
            entries.append((part, owned, need))
            if owned >= need:
                continue
            beneath = dbq.pieces_short_for(connection, part["unique_name"],
                                           need - owned)
            if beneath:
                entries.append((beneath, None, None))
                # The relics below answer for these, not for the gun itself.
                missing.extend(beneath)
            else:
                missing.append((part, need - owned))

        # The shared table, as tall as its rows. The hand-made one was
        # capped by a pixel maximum worked out before the dialog had a size,
        # and at 20pt the table below it was left with no height at all.
        pieces = simple_table(["Piece", "Held", "Needed"], len(entries))
        for row, (part, owned, need) in enumerate(entries):
            if owned is None:
                text = "needs " + ", ".join(
                    f"{piece['name']}" + (f" x{n}" if n > 1 else "")
                    for piece, n in part)
                line = QTableWidgetItem("      " + text)
                line.setForeground(QColor(theme.TEXT_DIM))
                pieces.setItem(row, 0, line)
                pieces.setSpan(row, 0, 1, 3)
                continue
            pieces.setItem(row, 0, QTableWidgetItem(part["name"]))
            count = QTableWidgetItem(held_text(connection, part["unique_name"])[1])
            # A surplus is its own state, not a completed one. It earns no
            # mastery, but a spare rare part is ducats and a second full set
            # sells - so it is marked rather than rounded away.
            count.setForeground(QColor(
                theme.ACCENT if owned > need else
                (theme.GOOD if owned == need else theme.WARN)))
            pieces.setItem(row, 1, count)
            needed = QTableWidgetItem(str(need))
            if owned > need:
                needed.setText(f"{need}   (+{owned - need} spare)")
                needed.setForeground(QColor(theme.ACCENT))
            pieces.setItem(row, 2, needed)
        layout.addWidget(pieces)

        if not missing:
            done = QLabel("Every piece is here — this set can be built.")
            done.setStyleSheet(f"color: {theme.GOOD};")
            layout.addWidget(done)
            layout.addStretch(1)
            fit_dialog(self, wide=(pieces,))
            return

        note = QLabel(
            "Relics that drop the missing pieces. \"Held\" counts what is in "
            "your inventory, at the refinement it is stored at — a relic you "
            "own is a relic you can open whether or not it still drops.")
        note.setObjectName("ViewSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)

        held_relics = {}
        for row in connection.execute(
                "SELECT r.name, SUM(o.count) AS held FROM owned_relics o "
                "JOIN relics r ON r.unique_name = o.relic_unique GROUP BY r.name"):
            held_relics[row["name"]] = int(row["held"] or 0)

        # Not fitted: a set can have a hundred of these, and the dialog is the
        # page here, so this is the one table that scrolls. `wrap` on the piece
        # name, which is the column that was cut to "PIEC" at 20pt.
        sources = simple_table(["Piece", "Relic", "Rarity", "Vault", "Held"],
                               0, stretch=0, fit=False, wrap=True, min_rows=6)
        rows = []
        for part, short in missing:
            for relic in dbq.relics_dropping(connection, part["unique_name"]):
                rows.append((part["name"], relic, held_relics.get(relic["name"], 0)))
        # What you already hold first, then what still drops, then the rest.
        # That is the order the question is asked in: can I open one now, can I
        # farm one, or is this a trade.
        rows.sort(key=lambda r: (-r[2], bool(r[1]["vaulted"]), r[0], r[1]["name"]))

        sources.setRowCount(len(rows))
        for row, (piece, relic, count) in enumerate(rows):
            sources.setItem(row, 0, QTableWidgetItem(piece))
            sources.setItem(row, 1, QTableWidgetItem(relic["name"]))
            sources.setItem(row, 2, QTableWidgetItem(str(relic["rarity"]).title()))
            vaulted = relic["vaulted"]
            mark = QTableWidgetItem(
                "unknown" if vaulted is None else
                ("vaulted" if vaulted else "available"))
            mark.setForeground(QColor(
                theme.TEXT_FAINT if vaulted is None else
                (theme.VAULTED if vaulted else theme.GOOD)))
            sources.setItem(row, 3, mark)
            owned = QTableWidgetItem(str(count) if count else "—")
            if count:
                owned.setForeground(QColor(theme.GOOD))
            sources.setItem(row, 4, owned)
        # Clicking a relic opens it. The question after "this relic drops the
        # piece I need" is always "and what else is in it" - a relic is opened
        # whole, so what else is inside decides whether the run is worth it.
        sources.setSelectionMode(QTableWidget.SingleSelection)
        sources.setSelectionBehavior(QTableWidget.SelectRows)
        sources.cellDoubleClicked.connect(
            lambda index, _column: self._open_relic(connection, rows, index))
        sources.setToolTip("Double-click a relic to see everything it drops")
        layout.addWidget(sources, 1)
        fit_dialog(self, wide=(pieces, sources), tall=sources)

    @staticmethod
    def _open_relic(connection, rows, index: int) -> None:
        from ui.widgets.relic_detail import RelicDetail

        if not 0 <= index < len(rows):
            return
        RelicDetail(connection, rows[index][1]["name"]).exec()
