"""What you can carry to the kiosk today, and what it is worth.

Split off the plan page for the same reason the fissure board was: that page
answers what the collection still needs, and this answers what to do with what
it does not. They read as opposites and were stacked on top of each other, with
the ducat total tucked into a folded heading — "750 ducats in spares" was the
number a player comes for and the last thing visible.

The page is one number and three lists that add up to it. Every list can be
emptied from the page itself, because a total that cannot be acted on goes
stale the first time it is acted on somewhere else.

Nothing here sells anything. The trade happens in a relay; this records that it
happened, which is the only half a tool outside the game can do.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets import thumbs
from ui.widgets.market_lookup import MarketLookup
from ui.widgets.section import Section
from ui.widgets.tables import (
    Sort, fit_height, order_by, simple_table as _table, sortable,
)
from wft.data import db as dbq


def _clickable(view, table, entries) -> None:
    """Open the item's detail when a row is chosen, except on the last column.

    The last column is the action. A page whose every row both opens a window
    and gives something away would be a page nobody clicks on twice.
    """
    from ui.widgets.item_detail import ItemDetail

    table.setCursor(Qt.PointingHandCursor)

    def open_row(row: int, column: int = 0):
        if column == table.columnCount() - 1:
            return
        if 0 <= row < len(entries) and entries[row] is not None:
            entry = entries[row]
            dialog = ItemDetail(view.conn, entry["unique"], entry["name"], view)
            dialog.exec()
            # The "keep one" mark moves what is spare.
            if dialog.kept_changed:
                view.refresh()

    table.cellClicked.connect(open_row)


#: What every row on this page can be told, in the same words each time. A
#: pile of spares handed to the kiosk, a set handed to another player and a
#: second built copy sold are three transactions and one fact: it left the
#: shelf. Three labels for it made the page read as three features.
GONE = "It\u2019s gone"
GONE_HEADER = "Gone?"


def _action(text: str = GONE) -> QTableWidgetItem:
    cell = QTableWidgetItem(text)
    cell.setForeground(QColor(theme.ACCENT))
    return cell


class DucatView(MarketLookup, QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = dbq.connect(cfg.db_path)
        dbq.assert_usable(self.conn)
        self.init_market_lookup()
        #: Set pieces that also appear in the spares list, by set.
        self._shared: dict[str, set] = {}
        #: How each table is sorted, one entry per table, kept here rather
        #: than on the widget because the page is rebuilt whole every time
        #: something leaves the shelf - an order living on the table would go
        #: back to the default every time the user traded anything.
        #:
        #: Alphabetical to begin with. It was ordered by ducats, which is the
        #: question the page answers, but not the question a person asks of it
        #: while holding it open: "where is X". Ordered by name that is one look;
        #: ordered by value it is a scan of fifty rows. The dearest pile is one
        #: click away either way, and the click is remembered.
        self._order = {name: {"column": 0, "descending": False}
                       for name in ("spares", "sets", "doubles")}
        # Built empty; `MainWindow.select` refreshes the page on the way in.
        self._build_ui()

    # -- plumbing ---------------------------------------------------------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("Ducats")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        subtitle = QLabel(
            "Everything you can hand to the kiosk without costing a set a "
            "piece. Checked against every recipe first.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.body = QVBoxLayout()
        self.body.setSpacing(theme.px(1.2))
        inner = QWidget()
        inner.setLayout(self.body)

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

    def _clear(self):
        # The market widgets belong to the rows about to be deleted; a stale
        # reference here would be a pointer into a destroyed widget the next
        # time a lookup finished.
        self._market_widgets = {}
        while self.body.count():
            child = self.body.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

    def _section(self, title: str, note: str, colour: str,
                 start_open: bool = True) -> QVBoxLayout:
        section = Section(title, note, colour, start_open)
        self.body.addWidget(section)
        return section.body

    def _faint(self, layout, text: str):
        label = QLabel(text)
        label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        label.setWordWrap(True)
        layout.addWidget(label)

    # -- the numbers ------------------------------------------------------
    def _spares(self):
        """Surplus parts, dearest pile first, with who they belong to.

        Each row carries why it is on offer. Two rows reading "x1" mean quite
        different things now: one is a copy past what the recipe wants, the
        other is the last piece of a set whose mastery is already banked - and
        the second is the one to see before handing it over,
        because the kiosk does not give anything back.
        """
        rows = []
        for row in dbq.spare_parts(self.conn):
            each = dbq.ducats(self.conn, row["unique_name"]) or 0
            # Only what the kiosk actually takes. Baro trades for Prime parts
            # and nothing else, so a mastered Inaros released its plain
            # blueprint onto a page that could only print a dash beside it -
            # a row proposing a trade nobody will make, so such a part does not
            # belong on the ducat list at all.
            #
            # Filtered here rather than in `spare_parts`, because the two
            # questions are different ones. The query answers what is surplus,
            # which stays true of a blueprint worth no ducats and is what the
            # inventory page lists it for; this page answers what a trip to
            # the kiosk is worth, and there a worthless row is noise.
            if not each:
                continue
            spare = int(row["spare"])
            owner = self.conn.execute(
                "SELECT i.unique_name, i.name FROM parts p "
                "JOIN items i ON i.unique_name = p.item_unique "
                "WHERE p.unique_name = ?", (row["unique_name"],)).fetchone()
            rows.append({
                "unique": row["unique_name"], "name": row["name"],
                "spare": spare, "each": each, "total": each * spare,
                "held": int(row["count"]),
                "kept": int(row["count"]) - spare,
                "freed": bool(row["freed_by_mastery"]),
                "built": bool(row["freed_by_build"]),
                "owner_unique": owner["unique_name"] if owner else None,
                "owner": ({"unique": owner["unique_name"], "name": owner["name"]}
                          if owner else None)})
        # The page's own order is applied where the table is filled, so that
        # a header click does not have to ask the database again. This is only
        # the order the list is handed over in.
        rows.sort(key=lambda r: r["name"].lower())
        return rows

    def _sellable_sets(self, survey):
        """Complete sets whose mastery is already banked."""
        priced = []
        for entry in survey:
            total = 0
            for part in self.conn.execute(
                    "SELECT unique_name, count_needed FROM parts "
                    "WHERE item_unique = ? AND alias_of IS NULL",
                    (entry["unique"],)):
                # The value of one set's own recipe, on purpose: a sum of
                # what building it takes, not a question of what to keep.
                total += (dbq.ducats(self.conn, part["unique_name"]) or 0) * int(
                    part["count_needed"])
            priced.append((entry, total))
        priced.sort(key=lambda pair: -pair[1])
        return priced

    def _survey(self):
        """Sets held whole, with nothing built, and the mastery already had."""
        out = []
        for row in self.conn.execute(
                "SELECT unique_name, name, category FROM items "
                "WHERE unique_name IN (SELECT DISTINCT item_unique FROM parts) "
                "AND category NOT IN ('reward_other', 'component_only') "
                "ORDER BY name"):
            held, total = dbq.set_progress(self.conn, row["unique_name"])
            if not total or held < total:
                continue
            if not dbq.is_mastered(self.conn, row["unique_name"]):
                continue
            out.append({"unique": row["unique_name"], "name": row["name"],
                        "category": row["category"] or "—",
                        "held": held, "total": total})
        return out

    def _doubles(self):
        """Finished copies held more than once, each with its verdict.

        The verdict is worked out here rather than while the cells are filled,
        because it is also what the column sorts on. Asked twice it would be
        two answers to keep in step, and the one on screen would be the one
        nobody checked.
        """
        rows = []
        for row in self.conn.execute(
                "SELECT i.unique_name, i.name, o.count FROM owned_items o "
                "JOIN items i ON i.unique_name = o.item_unique "
                "WHERE o.count > 1 ORDER BY i.name"):
            # Only while handing one over would leave a recipe short.
            consumers = (dbq.needed_as_ingredient(self.conn, row["unique_name"])
                         if dbq.sale_costs_copies(self.conn, row["unique_name"])
                         else [])
            names = ", ".join(f"{c['name']} (needs {c['count_needed']}x)"
                              for c in consumers)
            rows.append({
                "unique": row["unique_name"], "name": row["name"],
                "count": int(row["count"]), "needed": bool(consumers),
                "verdict": (f"still needed for {names} — do not sell blindly"
                            if consumers
                            else "not needed elsewhere — safe ducat fodder")})
        return rows

    # -- rendering --------------------------------------------------------
    def refresh(self):
        self._clear()
        spares = self._spares()
        sets = self._sellable_sets(self._survey())
        doubles = self._doubles()

        from_spares = sum(row["total"] for row in spares)
        from_sets = sum(ducats for _entry, ducats in sets)
        self._shared = self._pieces_in_both(spares, sets)
        self._headline(from_spares, from_sets, self._only_as_a_set(sets))

        self._spares_section(spares, from_spares)
        self._sets_section(sets, from_sets)
        self._doubles_section(doubles)
        self.body.addStretch(1)

    def _pieces_in_both(self, spares, sets) -> dict:
        """Which sets are also listed piece by piece, and which pieces those are.

        The two lists stopped being disjoint the day mastery began releasing a
        set's whole holding: a set that is complete and mastered has every
        piece released, so all of it appears above as loose parts as well.
        On a real shelf that is every mastered set - Orthos Prime,
        all three pieces - so the overlap is not an edge case to note, it is
        the normal case to account for.
        """
        by_unique = {row["unique"]: row for row in spares}
        shared: dict[str, set] = {}
        for entry, _ducats in sets:
            pieces = [row["unique_name"] for row in self.conn.execute(
                "SELECT unique_name FROM parts "
                "WHERE item_unique = ? AND alias_of IS NULL", (entry["unique"],))]
            overlap = {piece for piece in pieces if piece in by_unique}
            if overlap:
                shared[entry["unique"]] = overlap
        return shared

    def _only_as_a_set(self, sets) -> int:
        """Ducats reachable through the sets list and nowhere else.

        The headline adds the two lists together, and once they share pieces
        that says a number the shelf cannot pay: an Orthos Prime was counted
        once as three spare parts and again as a set. They are two ways to
        part with the same metal, not two piles.

        Counted per piece rather than per set, because a set can overlap in
        part. A released piece is always worth at least as much loose as it is
        inside a set - the spare count is the whole holding, the set asks only
        for what the recipe wants - so where the two meet, the spare row is
        already the larger number and the set adds nothing.
        """
        extra = 0
        for entry, ducats in sets:
            overlap = self._shared.get(entry["unique"], set())
            if not overlap:
                extra += ducats
                continue
            for row in self.conn.execute(
                    "SELECT unique_name, count_needed FROM parts "
                    "WHERE item_unique = ? AND alias_of IS NULL",
                    (entry["unique"],)):
                if row["unique_name"] in overlap:
                    continue
                extra += (dbq.ducats(self.conn, row["unique_name"]) or 0) * int(
                    row["count_needed"])
        return extra

    def _headline(self, from_spares: int, from_sets: int, only_as_a_set: int):
        """The number the page exists for, said once and said large.

        It was a fragment of a folded section heading, which is where a number
        goes to be missed. The two halves are named under it because they are
        not equally easy to collect: a pile of spare parts is one trip, and a
        full set is a set you have to be sure you will never build.

        `only_as_a_set` is what the sets list adds that the spares list does
        not already hold. Adding the two totals outright counted the pieces of
        every mastered set twice, and a headline that overstates the shelf is
        worse than none: this is the number a trip to the kiosk is decided on.
        """
        frame = QFrame()
        frame.setObjectName("Card")
        box = QVBoxLayout(frame)
        box.setContentsMargins(*theme.card_margins())

        total = QLabel(f"{from_spares + only_as_a_set} ducats")
        total.setStyleSheet(
            f"font-family: {theme.DISPLAY_FAMILY}; font-size: 2.2em; "
            f"font-weight: 600; color: {theme.GOOD};")
        box.addWidget(total)

        parts = []
        if from_spares:
            parts.append(f"{from_spares} in spare parts")
        if only_as_a_set:
            parts.append(f"{only_as_a_set} in mastered sets")
        detail = QLabel("  ·  ".join(parts) if parts
                        else "Nothing spare on the shelf.")
        detail.setObjectName("ViewSubtitle")
        detail.setWordWrap(True)
        box.addWidget(detail)

        # Said out loud rather than quietly subtracted. The sets below are
        # worth ducats too - they are simply the same pieces, counted once
        # already, and they can be handed over as parts or kept together
        # for another player. Naming the overlap is what makes the two lists
        # readable side by side.
        if from_sets and only_as_a_set < from_sets:
            shared = QLabel(
                f"{from_sets - only_as_a_set} of those ducats sit in pieces "
                f"that also form whole mastered sets below. Trade the parts "
                f"or sell the set — not both, so they count once here.")
            shared.setWordWrap(True)
            shared.setStyleSheet(f"color: {theme.TEXT_DIM};")
            box.addWidget(shared)
        self.body.addWidget(frame)

    def _spares_section(self, spares, total: int):
        freed = sum(1 for row in spares if row["freed"])
        layout = self._section(
            f"Spare parts — {total} ducats from {len(spares)} part(s)",
            "Copies a set no longer has a claim on. A recipe holds its count "
            "back while the set is still worth building; once its mastery is "
            "banked, the whole holding is surplus — unless the item is itself "
            "a piece of something not yet mastered, which keeps its own "
            "pieces on the shelf.",
            theme.GOOD)
        if not spares:
            self._faint(layout, "No part is held beyond what its set needs.")
            return

        # Why a row is on offer, in the row itself, before anything is
        # traded: "mastered" can mean a set that was levelled and sold, but it
        # can also mean one that was never built and might yet be wanted
        # - a Braton Prime with an Incarnon form is the case that costs a
        # re-farm. Both are surplus by the mastery rule; only one of them is
        # a decision, and the page should show which.
        if freed:
            note = QLabel(
                f"{freed} of these are free because their set is already "
                f"mastered, marked below. Nothing comes back from the kiosk, "
                f"so they are worth a second look if you might still build "
                f"one for its own sake.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {theme.TEXT_DIM};")
            layout.addWidget(note)

        # "Why" is the sentence, so it is the column that takes the slack and
        # wraps: left at its own width the table was 1320px at 20pt, wider than
        # the page in a 1600x900 window.
        table = _table(["Spare part", "Spare", "Why", "Each", "Total",
                        "Platinum", GONE_HEADER], len(spares), stretch=2,
                       wrap=True, scroll_x=True)
        columns = {
            0: Sort(lambda row: row["name"].lower()),
            1: Sort(lambda row: row["spare"], descending_first=True),
            2: Sort(lambda row: self._why_label(row)),
            3: Sort(lambda row: row["each"], descending_first=True),
            4: Sort(lambda row: row["total"], descending_first=True),
            5: Sort(lambda row: self._platinum_worth(row["unique"]),
                    descending_first=True),
        }
        # Held rather than rebuilt from `spares` on the spot, because the
        # click handlers below read this list by row index and it has to say
        # what the table says after every reordering.
        owners = [None] * len(spares)

        def fill():
            order_by(spares, columns, self._order["spares"])
            owners[:] = [row["owner"] for row in spares]
            for index, row in enumerate(spares):
                table.setItem(index, 0, QTableWidgetItem(row["name"]))
                table.setItem(index, 1, QTableWidgetItem(f"x{row['spare']}"))
                table.setItem(index, 2, self._why_cell(row))
                table.setItem(index, 3, QTableWidgetItem(
                    str(row["each"]) if row["each"] else "—"))
                value = QTableWidgetItem(
                    str(row["total"]) if row["total"] else "—")
                if row["total"]:
                    value.setForeground(QColor(theme.GOOD))
                table.setItem(index, 4, value)
                table.setItem(index, 5, self._platinum_cell(row["unique"]))
                table.setItem(index, 6, _action())

        fill()
        _clickable(self, table, owners)
        table.cellClicked.connect(
            lambda row, column: (self._traded_spare(spares[row])
                                 if column == 6 else None))
        table.setToolTip("Click a part to see its set, a heading to reorder, "
                         "or the last column once it has left the shelf")
        sortable(table, columns, self._order["spares"], fill)
        layout.addWidget(table)
        # The parts are the reason this button is here: a piece of a vaulted
        # set can be worth more to a player than the forty-five ducats the
        # kiosk pays, and until now the page could not say so; the market
        # prices show whether single parts are worth anything.
        layout.addWidget(self._market_row(
            [row["unique"] for row in spares], "spares"))

    def _why_cell(self, row) -> QTableWidgetItem:
        """One word for why this pile is on offer.

        Three states, because they are three different risks. A surplus copy
        is safe by construction. A set whose mastery is banked is safe by the
        mastery rule. A set that is also complete on the shelf is the
        one a player may want to sell whole instead, and it is listed twice on this
        page for that reason.
        """
        label = self._why_label(row)
        if label == "spare copy":
            cell = QTableWidgetItem(label)
            cell.setForeground(QColor(theme.TEXT_DIM))
            cell.setToolTip(
                f"{row['held']} held, {row['kept']} kept back for the recipe")
            return cell
        whole = label == "whole set"
        cell = QTableWidgetItem(label)
        cell.setForeground(QColor(theme.WARN if whole else theme.ACCENT))
        cell.setToolTip(
            "The set is complete and mastered — it is offered whole further "
            "down as well, and it is worth more to a player than to the kiosk"
            if whole else
            "Its set is built, and a built item is not traded, so nothing is "
            "kept back. Mastery comes from levelling the one you built."
            if row["built"] else
            "Its set is mastered, so nothing is kept back. Check you will not "
            "want to build one for its own sake.")
        return cell

    def _why_label(self, row) -> str:
        """The one word, decided in one place.

        The column can be sorted by it, so the word the cell shows and the
        word the order is taken from have to be the same word. Two readings of
        `_shared` would eventually disagree, and the disagreement would show
        up as rows that sort into a group they are not labelled with.
        """
        if not row["freed"]:
            return "spare copy"
        if row["built"]:
            return "set built"
        whole = any(row["unique"] in pieces
                    for pieces in getattr(self, "_shared", {}).values())
        return "whole set" if whole else "mastered"

    def _sets_section(self, priced, total: int):
        layout = self._section(
            f"Mastered sets — {total} ducats from {len(priced)} set(s)",
            "Every piece held, nothing built, and the mastery already banked. "
            "Building these earns no points, so the pieces are worth more at "
            "the kiosk than in the foundry — and the whole set is worth more "
            "to another player than to the kiosk.",
            theme.VAULTED)
        if not priced:
            self._faint(layout, "No mastered set is sitting complete on the "
                                "shelf.")
            return

        # Ducats are a fact of the game and platinum is a market, so the two
        # columns are read differently: one says what the kiosk pays, the other
        # what another player might.
        table = _table(["Set", "Category", "Pieces", "Ducats", "Platinum",
                        GONE_HEADER], len(priced))
        columns = {
            0: Sort(lambda pair: pair[0]["name"].lower()),
            1: Sort(lambda pair: pair[0]["category"].lower()),
            2: Sort(lambda pair: pair[0]["total"], descending_first=True),
            3: Sort(lambda pair: pair[1], descending_first=True),
            4: Sort(lambda pair: self._platinum_worth(pair[0]["unique"]),
                    descending_first=True),
        }
        entries = [None] * len(priced)

        def fill():
            order_by(priced, columns, self._order["sets"])
            entries[:] = [entry for entry, _ducats in priced]
            for index, (entry, ducats) in enumerate(priced):
                table.setItem(index, 0, QTableWidgetItem(entry["name"]))
                table.setItem(index, 1, QTableWidgetItem(entry["category"]))
                table.setItem(index, 2, QTableWidgetItem(str(entry["total"])))
                price = QTableWidgetItem(str(ducats))
                price.setForeground(QColor(theme.GOOD))
                table.setItem(index, 3, price)
                table.setItem(index, 4, self._platinum_cell(entry["unique"]))
                table.setItem(index, 5, _action())

        fill()
        _clickable(self, table, entries)
        table.cellClicked.connect(
            lambda row, column: (self._gave_away(priced[row][0])
                                 if column == 5 else None))
        table.setToolTip("Click a set to see it, a heading to reorder, or the "
                         "last column once it has left the shelf")
        sortable(table, columns, self._order["sets"], fill)
        layout.addWidget(table)
        layout.addWidget(self._market_row(
            [entry["unique"] for entry, _ducats in priced], "sets"))

    def _doubles_section(self, doubles):
        if not doubles:
            return
        layout = self._section(
            f"Built more than once — {len(doubles)}",
            "Finished items you hold several of. Whether one is spare depends "
            "on what else eats it, which is the column on the right.",
            theme.TEXT_DIM, start_open=bool(doubles))

        # The verdict is a sentence, so its column wraps like the "Why" column
        # of the spare table. Built component weapons such as Lex Prime
        # now sit here with "still needed for Aklex Prime (needs 2x) - do not
        # sell blindly", which at 20pt in a 1600x900 window was 6px too wide
        # and ended in "...".
        table = _table(["Item", "Built", "Verdict", GONE_HEADER],
                       len(doubles), stretch=2, wrap=True, scroll_x=True)
        table.setIconSize(thumbs.row_size(table))
        table.verticalHeader().setDefaultSectionSize(thumbs.row_height(table))
        columns = {
            0: Sort(lambda row: row["name"].lower()),
            1: Sort(lambda row: row["count"], descending_first=True),
            # Not the sentence, which starts with the same three words for
            # every item that is still wanted somewhere: the question this
            # column is sorted on is whether selling one costs a set.
            2: Sort(lambda row: (not row["needed"], row["verdict"].lower())),
        }
        owners = [None] * len(doubles)

        def fill():
            order_by(doubles, columns, self._order["doubles"])
            owners[:] = [{"unique": row["unique"], "name": row["name"]}
                         for row in doubles]
            for index, row in enumerate(doubles):
                name_cell = QTableWidgetItem(row["name"])
                thumbs.decorate_or_space(self.conn, name_cell, "item",
                                         row["unique"], table)
                table.setItem(index, 0, name_cell)
                table.setItem(index, 1, QTableWidgetItem(f"x{row['count']}"))
                verdict = QTableWidgetItem(row["verdict"])
                verdict.setForeground(QColor(
                    theme.WARN if row["needed"] else theme.TEXT_DIM))
                table.setItem(index, 2, verdict)
                table.setItem(index, 3, _action())

        fill()
        # Sized again now the rows are as tall as the pictures in them. The
        # table was measured when it was built, which was before it was told
        # how tall a row with a picture is - four pixels short a row, and the
        # last row of a long list went missing behind a scrollbar that is
        # deliberately switched off.
        fit_height(table)
        _clickable(self, table, owners)
        table.cellClicked.connect(
            lambda row, column: (self._traded_built(doubles[row])
                                 if column == 3 else None))
        table.setToolTip("Click an item to see it, a heading to reorder, or "
                         "the last column once one has left the shelf")
        sortable(table, columns, self._order["doubles"], fill)
        layout.addWidget(table)

    # -- the actions ------------------------------------------------------
    def _traded_spare(self, row) -> None:
        """A pile of spare parts went to the kiosk.

        The sentence under the numbers has to survive `keeping = 0`, which is
        now the common case: a mastered set keeps nothing back, and the old
        text promised "the 0 its recipe asks for stay on the shelf". Worse
        than clumsy - it read as reassurance exactly where there is none, on
        the rows where the last piece of a set is about to leave.
        """
        held = dbq.owned_count(self.conn, row["unique"])
        keeping = held - row["spare"]
        if keeping:
            fate = (f"The {keeping} its recipe asks for stay on the shelf.")
        elif row["built"]:
            fate = ("Nothing is kept back: its set is already built, and a "
                    "built item is not traded. Building another would earn "
                    "no mastery.")
        elif row["freed"]:
            fate = ("Nothing is kept back: its set is already mastered, so "
                    "the recipe has no further claim. Building one again "
                    "would mean farming the pieces afresh.")
        else:
            fate = "Nothing is kept back — this is the whole holding."
        answer = QMessageBox.question(
            self, f"{row['name']} — gone?",
            f"{row['name']}: {held} → {keeping}\n\n"
            f"{row['spare']} spare for {row['total']} ducats. {fate}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        dbq.trade_spare(self.conn, row["unique"], row["spare"], "manual")
        self.refresh()

    def _traded_built(self, row) -> None:
        """One finished copy went to the kiosk."""
        consumers = (dbq.needed_as_ingredient(self.conn, row["unique"])
                     if dbq.sale_costs_copies(self.conn, row["unique"]) else [])
        warning = ""
        if consumers:
            names = ", ".join(c["name"] for c in consumers)
            warning = (f"\n\nCareful: {names} is built out of this. Selling "
                       f"one may cost you that set.")
        answer = QMessageBox.question(
            self, f"{row['name']} — one gone?",
            f"{row['name']}: {row['count']} → {row['count'] - 1}\n\n"
            f"Mastery is kept.{warning}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No if consumers else QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        dbq.trade_built(self.conn, row["unique"], 1, "manual")
        self.refresh()

    def _gave_away(self, entry) -> None:
        """One whole set left the shelf without being built."""
        pieces = dbq.pieces_of_one_set(self.conn, entry["unique"], 1)
        if not pieces:
            QMessageBox.information(
                self, entry["name"],
                "No pieces of this set are recorded, so there is nothing to "
                "take off the shelf.")
            return
        listing = "\n".join(
            f"  {name}: {now} → {after}" for _kind, _unique, name, now, after
            in pieces)
        answer = QMessageBox.question(
            self, f"{entry['name']} — gone?",
            f"These come off the shelf:\n\n{listing}\n\n"
            f"Mastery is kept — the points were banked at rank 30.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        dbq.remove_one_set(self.conn, entry["unique"], 1, "manual")
        self.refresh()

    # -- the market -------------------------------------------------------
    def _platinum_cell(self, unique_name: str) -> QTableWidgetItem:
        """What the market last said, or a dash if it has not been asked."""
        from wft.market import client as market

        price = market.price_for(self.conn, unique_name)
        if price is None:
            cell = QTableWidgetItem("—")
            cell.setForeground(QColor(theme.TEXT_FAINT))
            return cell
        sell, buy = price["sell_min"], price["buy_max"]
        # The asking price, and what somebody is already offering. A sale is
        # usually worth something between them, and quoting only one of the two
        # would be a recommendation this tool has no business making.
        text = f"{sell}p" if sell is not None else "—"
        if buy is not None:
            text += f"   (bid {buy}p)"
        cell = QTableWidgetItem(text)
        cell.setForeground(QColor(theme.ACCENT))
        return cell

    def _platinum_worth(self, unique_name: str) -> int:
        """The asking price as a number, for the column to be sorted on.

        Unasked and untraded rows both come back as -1, so they gather at one
        end instead of scattering through the order. Cheap to call for every
        row: this reads what was already stored, and never opens a socket.
        """
        from wft.market import client as market

        price = market.price_for(self.conn, unique_name)
        if price is None or price["sell_min"] is None:
            return -1
        return int(price["sell_min"])
