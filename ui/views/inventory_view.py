"""Inventory: what is owned, what is doubled, and how to add to it.

Two axes matter here: parts held (`owned_parts`) and items already built
(`owned_items`). The game only ever shows the first one, which is exactly the
blind spot this tool exists to close.

The collection is genuinely empty on a fresh install (at most one manual
entry). Per the build instructions, an empty view has to explain how to fill
it rather than show a wall of 0/4 progress bars for eleven hundred items that
carry no information at all.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ui import file_dialogs, theme
from ui.widgets import progress_cell, thumbs
from ui.widgets.diamond import Diamond
from ui.widgets.name_suggest import suggest_on_typing
from ui.widgets.tables import (
    Sort, fit_height as _fit_height, order_by, simple_table as _table, sortable,
)
from wft.data import db as dbq

#: Categories held as currency or consumables rather than equipment. They have
#: to exist as relic rewards - Forma alone drops from 2180 relics - but an
#: inventory row reading "Kuva 1/1" is noise: Kuva is held in the thousands and
#: changes by the hour, and this list is about which sets are incomplete.
CURRENCY_CATEGORIES = dbq.UNCOUNTED_CATEGORIES


def _fill_spare_rows(table, scored) -> None:
    """The spare-parts rows, filled in whatever order they are in."""
    for i, (row, ducats, extra, value) in enumerate(scored):
        table.setItem(i, 0, QTableWidgetItem(row["name"]))
        table.setItem(i, 1, QTableWidgetItem(f"x{row['count']}"))
        kept = int(row["count"]) - extra
        kept_item = QTableWidgetItem(str(kept))
        if not kept:
            kept_item.setForeground(QColor(theme.TEXT_FAINT))
        table.setItem(i, 2, kept_item)
        spare_item = QTableWidgetItem(f"x{extra}")
        spare_item.setForeground(QColor(theme.WARN))
        table.setItem(i, 3, spare_item)
        why = QTableWidgetItem(
            ("set built" if row["freed_by_build"] else "set mastered")
            if row["freed_by_mastery"] else "spare copy")
        why.setForeground(QColor(
            theme.ACCENT if row["freed_by_mastery"] else theme.TEXT_DIM))
        why.setToolTip(
            f"Its recipe asks for {row['count_needed']}, but the set "
            + ("is built and nothing else is built from it"
               if row["freed_by_build"] else
               "is mastered and nothing else is built from it")
            if row["freed_by_mastery"] else
            f"Its recipe asks for {row['count_needed']}")
        table.setItem(i, 4, why)
        table.setItem(i, 5, QTableWidgetItem(str(ducats) if ducats else "-"))
        value_item = QTableWidgetItem(str(value) if value else "-")
        if value:
            value_item.setForeground(QColor(theme.GOOD))
        table.setItem(i, 6, value_item)


class InventoryView(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = dbq.connect(cfg.db_path)
        dbq.assert_usable(self.conn)
        #: The order of each table on this page, kept here rather than on the
        #: widgets: every one of these lists is rebuilt after an import, and
        #: an order living on a table would be thrown away with it.
        self._order = {"sets": {"column": 2, "descending": True},
                       "spares": {"column": 6, "descending": True},
                       "doubles": {"column": 0, "descending": False}}
        # Built empty; the Collection page refreshes both of its hosts when
        # `MainWindow.select` brings it in.
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("Inventory")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        self.progress_page = self._build_progress_page()
        self.tabs.addTab(self.progress_page, "Progress")

        self.duplicates_page = self._build_duplicates_page()
        self.tabs.addTab(self.duplicates_page, "Duplicates")

        self.manage_page = self._build_manage_page()
        self.tabs.addTab(self.manage_page, "Add and correct")

    # -- progress tab ----------------------------------------------------
    def _build_progress_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, theme.px(0.5), 0, 0)

        self.progress_stack = QVBoxLayout()
        layout.addLayout(self.progress_stack)
        return page

    def _render_progress(self):
        while self.progress_stack.count():
            child = self.progress_stack.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

        summary = dbq.inventory_summary(self.conn)
        if not summary["parts"] and not summary["items"]:
            self.progress_stack.addWidget(self._empty_state())
            return

        placeholders = ",".join("?" * len(CURRENCY_CATEGORIES))
        rows = self.conn.execute(
            f"SELECT i.unique_name, i.name, i.category FROM items i "
            f"WHERE i.unique_name IN (SELECT DISTINCT item_unique FROM parts) "
            f"AND i.category NOT IN ({placeholders}) "
            f"ORDER BY i.name", CURRENCY_CATEGORIES).fetchall()

        entries = []
        for row in rows:
            held, total = dbq.set_progress(self.conn, row["unique_name"])
            built = dbq.item_built(self.conn, row["unique_name"])
            if held == 0 and built == 0:
                continue
            entries.append((row, held, total, built))

        if not entries:
            hint = QLabel(
                "Nothing tracked has parts or a finished copy recorded yet. "
                "Use Collection → Add and correct, or import a recording "
                "on the Import page.")
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {theme.TEXT_DIM}; padding: {theme.px(1.6)}px;")
            self.progress_stack.addWidget(hint)
            return

        ready = [e for e in entries if e[2] and e[1] >= e[2] and not e[3]]

        stale = self._stale_notice()
        if stale is not None:
            self.progress_stack.addWidget(stale)
        self.progress_stack.addWidget(self._stats_strip(entries))
        if ready:
            self.progress_stack.addWidget(self._ready_box(ready))

        # The whole page is this table, so it is the thing that scrolls
        # rather than being sized to four hundred rows of its own.
        table = _table(["Item", "Category", "Parts held", "Built"],
                       len(entries), fit=False)
        columns = {
            0: Sort(lambda e: e[0]["name"].casefold()),
            1: Sort(lambda e: (e[0]["category"] or "").casefold()),
            # Complete sets first: a set with every part is a thing to act on,
            # not just a thing to look at. Then partial sets by how close they
            # are, which is exactly what the bar in this column draws.
            2: Sort(lambda e: (e[1] / e[2] if e[2] else (1 if e[3] else 0)),
                    descending_first=True),
            3: Sort(lambda e: e[3], descending_first=True),
        }
        # The set progress is drawn as a bar. "3/4" is a thing you read; three
        # quarters of a bar is a thing you see, and this page is nothing but
        # that question repeated four hundred times.
        table.setItemDelegateForColumn(2, progress_cell.ProgressDelegate(table))
        # The artwork beside the name. A list of names is a database; a list of
        # names with the thing next to each one is an inventory, and the
        # pictures are already here - cut from the user's own captures at
        # import.
        table.setIconSize(thumbs.row_size(table))
        table.verticalHeader().setDefaultSectionSize(thumbs.row_height(table))
        # Double-click opens the set: which piece is missing, what drops it,
        # and whether one of those relics is already in the inventory. The
        # table alone says how far along a set is and nothing about what to do
        # next, which is the question that follows every time.
        table.setToolTip("Double-click a row to see what the set still "
                         "needs, or click a heading to reorder the list.")
        table.cellDoubleClicked.connect(
            lambda row, _col, rows=entries: self._open_detail(rows[row][0]))
        def fill():
            order_by(entries, columns, self._order["sets"],
                     tiebreak=lambda e: e[0]["name"].casefold())
            for i, (row, held, total, built) in enumerate(entries):
                name_item = QTableWidgetItem(row["name"])
                thumbs.decorate(self.conn, name_item, "item",
                                row["unique_name"])
                table.setItem(i, 0, name_item)
                table.setItem(i, 1, QTableWidgetItem(row["category"]))
                progress_item = QTableWidgetItem()
                progress_cell.set_progress(progress_item, held, total)
                table.setItem(i, 2, progress_item)
                built_item = QTableWidgetItem(f"x{built}" if built else "-")
                if built:
                    built_item.setForeground(QColor(theme.GOOD))
                table.setItem(i, 3, built_item)

        fill()
        sortable(table, columns, self._order["sets"], fill)
        progress_cell.fit_column(table, 2)
        self.progress_stack.addWidget(table, 1)

    def _open_detail(self, row):
        from ui.widgets.item_detail import ItemDetail

        dialog = ItemDetail(self.conn, row["unique_name"], row["name"], self)
        dialog.exec()
        # The "keep one" mark moves what is spare and how far a set is.
        if dialog.kept_changed:
            self.refresh()

    def _clear_layout(self, layout):
        """Empty a layout so nothing of the old contents is left showing.

        `deleteLater` alone is not enough. It removes the widget from the
        layout immediately but destroys it only on the next event loop pass,
        and in between it is still a visible child of the same parent - sitting
        at the coordinates the layout last gave it. Switching from one item to
        another drew the new detail over the remains of the old one.

        Reparenting to None takes it out of the widget tree at once; the
        deferred delete then only frees it.
        """
        while layout.count():
            child = layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _stale_notice(self) -> QWidget | None:
        """Say so when the stored numbers were read by an older version.

        A recording is read once and its results live here until someone
        imports again, so a fix to the reader does not reach what is already
        written. Without this the page shows corrected and uncorrected numbers
        side by side and looks equally sure of both — and the duplicates tab
        offers the uncorrected ones as ducat fodder, which is advice to sell.
        """
        from wft.inventory import scan as scan_mod

        summary = dbq.inventory_summary(self.conn)
        if not summary["parts"].get("video") and not summary["items"].get("video"):
            return None
        stored = dbq.scan_revision(self.conn)
        if stored is not None and stored >= scan_mod.SCAN_REVISION:
            return None

        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.3))

        top = QHBoxLayout()
        top.setSpacing(2)
        top.addWidget(Diamond(theme.WARN))
        header = QLabel("These numbers were read by an older version")
        header.setStyleSheet(f"color: {theme.WARN}; font-weight: 600;")
        top.addWidget(header)
        top.addStretch(1)
        layout.addLayout(top)

        body = QLabel(
            "Stack counts in particular: a fixed piece of tile artwork used to "
            "be read as a count on things that do not stack, so a Warframe can "
            "appear here four times over. Re-import the recording to replace "
            "them — nothing else needs doing.")
        body.setWordWrap(True)
        body.setObjectName("ViewSubtitle")
        layout.addWidget(body)
        return frame

    def _stats_strip(self, entries) -> QWidget:
        """Totals and a per-category breakdown, the way an earlier attempt opened.

        A list of rows answers "what about this item"; it does not answer "how
        far along am I", which is the question someone opens the page with.
        """
        by_category: dict[str, list[int]] = {}
        complete = partial = built_total = 0
        for row, held, total, built in entries:
            bucket = by_category.setdefault(row["category"], [0, 0])
            bucket[1] += 1
            if total and held >= total:
                bucket[0] += 1
                complete += 1
            elif held:
                partial += 1
            built_total += built

        frame = QFrame()
        frame.setObjectName("StatsStrip")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(theme.px(0.95), theme.px(0.8), theme.px(0.95), theme.px(0.8))
        layout.setSpacing(theme.px(0.7))

        top = QHBoxLayout()
        top.setSpacing(theme.px(1.8))
        for value, label in ((complete, "sets complete"),
                             (partial, "sets partial"),
                             (built_total, "copies built"),
                             (len(entries), "items touched")):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            number = QLabel(str(value))
            number.setObjectName("StatNumber")
            caption = QLabel(label)
            caption.setObjectName("StatCaption")
            cell.addWidget(number)
            cell.addWidget(caption)
            top.addLayout(cell)
        top.addStretch(1)
        layout.addLayout(top)

        ordered = sorted(by_category.items(), key=lambda kv: -kv[1][1])
        line = "   ".join(f"{name} {done}/{seen}" for name, (done, seen) in ordered)
        breakdown = QLabel(line)
        breakdown.setObjectName("StatBreakdown")
        breakdown.setWordWrap(True)
        layout.addWidget(breakdown)
        return frame

    def _ready_box(self, ready) -> QWidget:
        """Sets holding every part, with nothing built yet.

        This is the one row on the page that is an instruction rather than a
        record: the parts are sitting in the foundry doing nothing.
        """
        frame = QFrame()
        frame.setObjectName("ReadyBox")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.3))

        top = QHBoxLayout()
        top.setSpacing(2)
        top.addWidget(Diamond(theme.GOOD))
        header = QLabel(f"Ready to build — {len(ready)} complete set(s)")
        header.setObjectName("ReadyHeader")
        top.addWidget(header)
        top.addStretch(1)
        layout.addLayout(top)

        names = ", ".join(sorted(row["name"] for row, _h, _t, _b in ready))
        body = QLabel(names)
        body.setWordWrap(True)
        body.setObjectName("ReadyBody")
        layout.addWidget(body)
        return frame

    def _empty_state(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.7))

        heading = QLabel("Nothing recorded yet")
        heading.setObjectName("CardHeading")
        layout.addWidget(heading)

        text = QLabel(
            "The inventory only knows what you tell it. The game shows what is "
            "in your pockets and never what you have already built, so a part "
            "you spent looks the same as one you never had. There are two ways "
            "to fill it in:\n\n"
            "1. Add entries by hand under Collection → Add and correct — quick for a handful "
            "of items you already know you have or have built.\n\n"
            "2. Record a screen capture of your inventory and read it in on the "
            "Import page — the way to capture everything at once, including "
            "duplicates and quantities.\n\n"
            "An empty list here is not a bug: it means nothing has been recorded, "
            "not that you own nothing.")
        text.setWordWrap(True)
        text.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 1.000em;")
        layout.addWidget(text)
        layout.addStretch(1)
        return frame

    # -- duplicates tab ----------------------------------------------------
    def _build_duplicates_page(self) -> QWidget:
        # Both sections are tables inside a scroll area. They were stacked
        # labels before, one per duplicate, and with a real collection behind
        # them the labels ran past the bottom of the page and drew over each
        # other - the page had no way to scroll and the labels no room to sit in.
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, theme.px(0.55), theme.px(0.8), 0)
        layout.setSpacing(theme.px(0.95))

        # Not only duplicates any more: a mastered set releases its whole
        # holding, so a part held once can stand here with nothing kept back.
        parts_header = QLabel("Parts free to trade — extras worth ducats")
        parts_header.setObjectName("SectionHeader")
        layout.addWidget(parts_header)
        self.dup_parts_stack = QVBoxLayout()
        layout.addLayout(self.dup_parts_stack)

        items_header = QLabel("Duplicate built items — check before you sell")
        items_header.setObjectName("SectionHeader")
        layout.addWidget(items_header)
        self.dup_items_stack = QVBoxLayout()
        layout.addLayout(self.dup_items_stack)

        layout.addStretch(1)

        page = QScrollArea()
        page.setWidget(inner)
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        return page

    def _render_duplicates(self):
        for stack in (self.dup_parts_stack, self.dup_items_stack):
            while stack.count():
                child = stack.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()

        # Past what the set still asks for, not past one. A component a recipe
        # wants twice is not a duplicate at two - and once a set's mastery is
        # banked the recipe asks for nothing at all, which is why the column
        # below reports what is actually kept back rather than what the recipe
        # says. Reading the recipe there produced rows saying "You have x1,
        # Set needs 1, Spare x1", which is three numbers that cannot all be
        # true at once.
        dup_parts = dbq.spare_parts(self.conn)

        if not dup_parts:
            hint = QLabel("No part is held beyond what its set needs.")
            hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
            self.dup_parts_stack.addWidget(hint)
        else:
            scored = []
            for row in dup_parts:
                ducats = dbq.ducats(self.conn, row["unique_name"]) or 0
                extra = int(row["spare"])
                scored.append((row, ducats, extra, ducats * extra))
            table = _table(["Part", "You have", "Kept back", "Spare", "Why",
                            "Ducats each", "Sellable value"], len(scored))
            columns = {
                0: Sort(lambda s: s[0]["name"].casefold()),
                1: Sort(lambda s: s[0]["count"], descending_first=True),
                2: Sort(lambda s: int(s[0]["count"]) - s[2],
                        descending_first=True),
                3: Sort(lambda s: s[2], descending_first=True),
                4: Sort(lambda s: bool(s[0]["freed_by_mastery"])),
                5: Sort(lambda s: s[1], descending_first=True),
                6: Sort(lambda s: s[3], descending_first=True),
            }

            def fill_parts():
                order_by(scored, columns, self._order["spares"],
                         tiebreak=lambda s: s[0]["name"].casefold())
                _fill_spare_rows(table, scored)

            fill_parts()
            sortable(table, columns, self._order["spares"], fill_parts)
            self.dup_parts_stack.addWidget(_fit_height(table))

        dup_items = self.conn.execute(
            "SELECT i.unique_name, i.name, o.count FROM owned_items o "
            "JOIN items i ON i.unique_name = o.item_unique WHERE o.count > 1"
        ).fetchall()

        if not dup_items:
            hint = QLabel("No duplicate built items recorded yet.")
            hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
            self.dup_items_stack.addWidget(hint)
        else:
            # Worked out before the rows are laid down, because it is also
            # what the verdict column is sorted on. Asked twice it would be
            # two answers to keep in step.
            judged = []
            for row in dup_items:
                consumers = (dbq.needed_as_ingredient(self.conn,
                                                      row["unique_name"])
                             if dbq.sale_costs_copies(self.conn, row["unique_name"])
                             else [])
                names = ", ".join(f"{c['name']} (needs {c['count_needed']}x)"
                                  for c in consumers)
                judged.append((row, bool(consumers),
                               f"still needed for {names} — do not sell blindly"
                               if consumers else
                               "not needed elsewhere — safe ducat fodder"))

            # Named apart from the spare-parts table above it. Both live
            # in this one method, and `table` and `columns` reused for the
            # second one were rebound under the first one's fill - which then
            # wrote its parts into the duplicates table on the first click.
            # It is exactly the fault the sort was written to avoid, one
            # scope further out.
            # The verdict is the long column here, not the name.
            items_table = _table(["Item", "Built", "Verdict"], len(judged),
                                 stretch=2)
            items_columns = {
                0: Sort(lambda j: j[0]["name"].casefold()),
                1: Sort(lambda j: j[0]["count"], descending_first=True),
                # Not the sentence, which opens with the same three words for
                # every item that is wanted somewhere: what this column is
                # asked is whether selling one costs a set.
                2: Sort(lambda j: (not j[1], j[2].casefold())),
            }

            def fill_items():
                order_by(judged, items_columns, self._order["doubles"],
                         tiebreak=lambda j: j[0]["name"].casefold())
                for i, (row, needed, verdict_text) in enumerate(judged):
                    items_table.setItem(i, 0, QTableWidgetItem(row["name"]))
                    items_table.setItem(i, 1,
                                        QTableWidgetItem(f"x{row['count']}"))
                    verdict = QTableWidgetItem(verdict_text)
                    verdict.setForeground(QColor(theme.WARN if needed
                                                 else theme.TEXT_DIM))
                    items_table.setItem(i, 2, verdict)

            fill_items()
            sortable(items_table, items_columns, self._order["doubles"],
                     fill_items)
            self.dup_items_stack.addWidget(_fit_height(items_table))

    # -- manage tab --------------------------------------------------------
    def _build_manage_page(self) -> QWidget:
        inner = QWidget()
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(0, theme.px(0.5), 0, 0)
        outer.setSpacing(theme.px(1.1))

        add_frame = QFrame()
        add_frame.setObjectName("Card")
        add_layout = QVBoxLayout(add_frame)
        add_layout.setContentsMargins(*theme.card_margins())
        add_layout.setSpacing(theme.px(0.7))

        add_header = QLabel("Add or update an entry")
        add_header.setObjectName("CardHeading")
        add_layout.addWidget(add_header)

        form_row = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(
            "Name, e.g. Rhino Prime Systems or Meso B9 Relic [Radiant]")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(0, 999)
        self.count_spin.setValue(1)
        # Asked out loud the first time it was used: is this plus one, or
        # equal to one? The field is a total, and nothing on the form said so.
        self.count_spin.setToolTip("How many you hold in total, not how many "
                                   "to add")
        self.count_spin.setPrefix("= ")
        add_btn = QPushButton("Save")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add_entry)
        # The name is the field that needs room; a count of at most three
        # figures and a button with two words do not. Stretch weights gave the
        # count a third of the width of the thing it counts.
        # Never under what the box itself needs: "= 999" and its buttons want
        # 164px at 20pt, the sum of six digits and 40px was 130.
        digits = self.count_spin.fontMetrics().horizontalAdvance("0")
        self.count_spin.setMaximumWidth(max(
            digits * 6 + 40, self.count_spin.minimumSizeHint().width()))
        form_row.addWidget(self.name_edit, 1)
        form_row.addWidget(self.count_spin)
        form_row.addWidget(add_btn)
        add_layout.addLayout(form_row)

        # Shown only for a name that is both a part and a different built
        # item. Nothing is guessed: Save waits for a choice.
        self.kind_row = QWidget()
        kind_layout = QHBoxLayout(self.kind_row)
        kind_layout.setContentsMargins(0, 0, 0, 0)
        kind_layout.addWidget(QLabel("Save as:"))
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Choose...", None)
        self.kind_combo.addItem("Part", "part")
        self.kind_combo.addItem("Built item", "item")
        self.kind_combo.currentIndexChanged.connect(self._show_holding)
        kind_layout.addWidget(self.kind_combo)
        kind_layout.addStretch(1)
        self.kind_row.setVisible(False)
        self._ambiguous = False
        add_layout.addWidget(self.kind_row)

        # What is on record for the name being typed, and the field set to it.
        # A total is the right thing to store and the wrong thing to ask for
        # cold: "found one Lith Q3" means nothing until you know whether two
        # or twenty are already recorded, and nobody knows that by heart. So
        # the form answers it, and adding one becomes one press of the arrow.
        self.holding_label = QLabel("")
        self.holding_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        add_layout.addWidget(self.holding_label)
        #: The entry the count field was last filled in for, so typing on does
        #: not keep resetting a number the user has since chosen.
        self._filled_for: tuple[str, str] | None = None
        self.name_edit.textChanged.connect(self._name_changed)

        self.add_feedback = QLabel(
            "Type a part, a finished item or a relic; the form works out "
            "which. The number is the new total, not an addition: found a "
            "third Lith Q3 and holding two, enter 3. A count of 0 removes the "
            "entry. Built items are worth entering: the game cannot show you "
            "what you have already spent parts on. A relic without a "
            "refinement in brackets is taken as Intact — write it as it reads "
            "on the tile, e.g. Meso B9 Relic [Radiant].")
        self.add_feedback.setWordWrap(True)
        self.add_feedback.setStyleSheet(f"color: {theme.TEXT_DIM};")
        add_layout.addWidget(self.add_feedback)

        outer.addWidget(add_frame)
        self._reload_completer()

        # Mastery by hand. Two cases need it and neither can be scanned:
        # an item mastered and sold before this tool existed leaves nothing on
        # screen to read, and a misread tile latches the wrong item forever.
        # The scan only ever sets the flag, so this is the only way to clear it.
        mastery_frame = QFrame()
        mastery_frame.setObjectName("Card")
        mastery_layout = QVBoxLayout(mastery_frame)
        mastery_layout.setContentsMargins(*theme.card_margins())
        mastery_layout.setSpacing(theme.px(0.7))

        mastery_header = QLabel("Mastery")
        mastery_header.setObjectName("CardHeading")
        mastery_layout.addWidget(mastery_header)

        mastery_row = QHBoxLayout()
        self.mastery_edit = QLineEdit()
        self.mastery_edit.setPlaceholderText("Item, e.g. Rhino Prime")
        mark_btn = QPushButton("Mark as mastered")
        mark_btn.setObjectName("Primary")
        mark_btn.clicked.connect(lambda: self._set_mastery(True))
        unmark_btn = QPushButton("Clear")
        unmark_btn.clicked.connect(lambda: self._set_mastery(False))
        mastery_row.addWidget(self.mastery_edit, 3)
        mastery_row.addWidget(mark_btn, 1)
        mastery_row.addWidget(unmark_btn, 1)
        mastery_layout.addLayout(mastery_row)

        self.mastery_feedback = QLabel(
            "Mastery is kept when an item is sold, so an import can set this "
            "but never take it back. Anything levelled and sold before the "
            "first scan has to be entered here.")
        self.mastery_feedback.setWordWrap(True)
        self.mastery_feedback.setStyleSheet(f"color: {theme.TEXT_DIM};")
        mastery_layout.addWidget(self.mastery_feedback)

        self._item_names = [r["name"] for r in self.conn.execute(
            "SELECT name FROM items ORDER BY name")]
        self._mastery_model = suggest_on_typing(
            self.mastery_edit, lambda: self._item_names)

        # The profile import moved to the Import page. Bulk imports belong
        # together: it is the one import that is not a scan, and having it
        # here meant the user had to be told where it was.
        pointer = QLabel(
            "Mastery for items you levelled and sold is imported from your "
            "Warframe profile — on the Import page.")
        pointer.setWordWrap(True)
        pointer.setStyleSheet(f"color: {theme.TEXT_DIM};")
        mastery_layout.addWidget(pointer)

        outer.addWidget(mastery_frame)

        # Source breakdown + rollback, so a bad video import is not permanent.
        sources_frame = QFrame()
        sources_frame.setObjectName("Card")
        sources_layout = QVBoxLayout(sources_frame)
        sources_layout.setContentsMargins(*theme.card_margins())
        sources_layout.setSpacing(theme.px(0.7))

        sources_header = QLabel("Recorded by source")
        sources_header.setObjectName("CardHeading")
        sources_layout.addWidget(sources_header)

        self.sources_label = QLabel("")
        self.sources_label.setWordWrap(True)
        self.sources_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        sources_layout.addWidget(self.sources_label)

        clear_row = QHBoxLayout()
        self.clear_combo = QComboBox()
        self.clear_combo.addItems(["manual", "video", "pick", "test"])
        clear_btn = QPushButton("Remove everything from this source")
        clear_btn.clicked.connect(self._clear_source)
        clear_row.addWidget(self.clear_combo)
        clear_row.addWidget(clear_btn)
        clear_row.addStretch(1)
        sources_layout.addLayout(clear_row)

        outer.addWidget(sources_frame)

        # The holdings are the only thing here that cannot be
        # regenerated. Manifests reload in seconds; a recording takes minutes
        # to make and minutes to read, and a hand correction is gone for good.
        # Both previous attempts had a backup and this did not.
        backup_frame = QFrame()
        backup_frame.setObjectName("Card")
        backup_layout = QVBoxLayout(backup_frame)
        backup_layout.setContentsMargins(*theme.card_margins())
        backup_layout.setSpacing(theme.px(0.7))

        backup_header = QLabel("Backup")
        backup_header.setObjectName("CardHeading")
        backup_layout.addWidget(backup_header)

        backup_row = QHBoxLayout()
        export_btn = QPushButton("Save a backup...")
        export_btn.clicked.connect(self._export_holdings)
        import_btn = QPushButton("Restore from a backup...")
        import_btn.clicked.connect(self._import_holdings)
        backup_row.addWidget(export_btn)
        backup_row.addWidget(import_btn)
        backup_row.addStretch(1)
        backup_layout.addLayout(backup_row)

        self.backup_feedback = QLabel(
            "Parts, built items, mastery, relics and the readings you have "
            "dismissed — everything you put in, and nothing that can be "
            "rebuilt from the manifests. Restoring replaces what is recorded.")
        self.backup_feedback.setWordWrap(True)
        self.backup_feedback.setStyleSheet(f"color: {theme.TEXT_DIM};")
        backup_layout.addWidget(self.backup_feedback)

        outer.addWidget(backup_frame)
        outer.addStretch(1)

        # Four cards that are 1456px tall at 20pt in a window 1108px tall. With
        # nowhere to scroll, Qt squeezed every row below its text: the entry
        # field had 24px of 75, the radio buttons 24 of 37, and the headings
        # were cut through the middle. It scrolls instead, as the
        # Duplicates tab beside it and the Plan page do.
        page = QScrollArea()
        page.setWidget(inner)
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        return page

    def _export_holdings(self):
        from datetime import date
        from wft.data import backup as backup_mod

        path = file_dialogs.save_file(
            self, "Save the inventory", f"warframe-inventory-{date.today()}.json",
            "Backup (*.json);;All files (*)")
        if not path:
            return
        try:
            counts = backup_mod.write(self.conn, path)
        except OSError as exc:
            self.backup_feedback.setText(f"Could not write it: {exc}")
            return
        self.backup_feedback.setText(
            f"Saved {counts['parts']} part(s), {counts['items']} item(s), "
            f"{counts['relics']} relic(s) and {counts['dismissed']} dismissed "
            f"reading(s) to {path}.")

    def _import_holdings(self):
        """Put a backup back, after saying what is about to be lost.

        Replacing is what restoring a backup means, and it is also the one
        action here that destroys work — a scan takes minutes and mastery
        cannot be re-scanned at all. So the counts on both sides are shown
        before anything is written.
        """
        from wft.data import backup as backup_mod

        path = file_dialogs.open_file(
            self, "A backup written by this tool", "",
            "Backup (*.json);;All files (*)")
        if not path:
            return
        try:
            data = backup_mod.read(path)
        except ValueError as exc:
            self.backup_feedback.setText(str(exc))
            return

        current = backup_mod.export_holdings(self.conn)["counts"]
        incoming = data.get("counts", {})
        reply = QMessageBox.question(
            self, "Replace what is recorded?",
            f"Recorded now: {current['parts']} part(s), {current['items']} "
            f"item(s), {current['relics']} relic(s).\n"
            f"In the backup: {incoming.get('parts', '?')} part(s), "
            f"{incoming.get('items', '?')} item(s), "
            f"{incoming.get('relics', '?')} relic(s), written "
            f"{data.get('written_at', 'at an unknown time')}.\n\n"
            f"Restoring replaces what is recorded. Mastery cannot be "
            f"re-scanned, so save a backup first if you are unsure.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        written = backup_mod.restore(self.conn, data)
        self.refresh()
        self.backup_feedback.setText(
            f"Restored {written['parts']} part(s), {written['items']} item(s), "
            f"{written['relics']} relic(s) and {written['dismissed']} dismissed "
            f"reading(s).")

    def _set_mastery(self, mastered: bool):
        """Latch or unlatch one item, by exact name.

        Exact rather than fuzzy on purpose: "Braton" and "Braton Prime" are
        different items with different mastery, and guessing between them is
        precisely the mistake this control exists to correct.
        """
        name = self.mastery_edit.text().strip()
        if not name:
            self.mastery_feedback.setText("Type an item name first.")
            return
        row = self.conn.execute(
            "SELECT unique_name, name FROM items WHERE name = ? COLLATE NOCASE",
            (name,)).fetchone()
        if row is None:
            near = dbq.name_suggestions(name, self._item_names, limit=5)
            self.mastery_feedback.setText(
                f"No item called {name!r}."
                + (f" Did you mean: {', '.join(near)}?" if near else ""))
            return
        dbq.set_item_mastered_manually(self.conn, row["unique_name"], mastered)
        done, total = dbq.mastered_count(self.conn)
        state = "mastered" if mastered else "not mastered"
        self.mastery_feedback.setText(
            f"{row['name']} is now {state}. {done} of {total} tracked items.")
        self.mastery_edit.clear()
        self.refresh()

    def _reload_completer(self):
        # Parts, finished items and every relic refinement in one list. A
        # relic is offered once per refinement, spelled the way the game
        # prints it on the tile, so what the user types and what they read are
        # the same string. Loaded once here; each keystroke only filters it.
        names = [r["name"] for r in self.conn.execute(
            "SELECT name FROM parts WHERE alias_of IS NULL")]
        names += [r["name"] for r in self.conn.execute("SELECT name FROM items")]
        names += [dbq.relic_label(r["name"], dbq.refinement_of(r["unique_name"]),
                                  always=True)
                  for r in self.conn.execute(
                      "SELECT name, unique_name FROM relics")]
        # Forma and its kind are never counted, so never offered.
        self._uncounted = {n.casefold() for n in dbq.uncounted_names(self.conn)}
        self._entry_names = sorted(
            {n for n in names if n.casefold() not in self._uncounted},
            key=str.casefold)
        if getattr(self, "_name_model", None) is None:
            self._name_model = suggest_on_typing(
                self.name_edit, lambda: self._entry_names)

    def _name_changed(self, *_args) -> None:
        # A choice made for one name does not carry over to the next.
        self.kind_combo.blockSignals(True)
        self.kind_combo.setCurrentIndex(0)
        self.kind_combo.blockSignals(False)
        self._show_holding()

    #: How each kind is named in the holding line and in the feedback.
    _KIND_WORDS = {"part": "part", "item": "built item", "relic": "relic"}

    def _resolve_entry(self, name: str):
        """(kind, row) the typed name means, None if nothing or not yet chosen.

        Sets `self.kind_row` as a side effect: visible only while the name is
        ambiguous. The holding line and Save both come through here.
        """
        found = dbq.entry_candidates(self.conn, name)
        ambiguous = len(found) > 1
        self._ambiguous = ambiguous
        self.kind_row.setVisible(ambiguous)
        if not ambiguous:
            return found[0] if found else None
        wanted = self.kind_combo.currentData()
        return dbq.find_entry(self.conn, name, wanted) if wanted else None

    def _entry_label(self, kind: str, row) -> str:
        if kind == "relic":
            return dbq.relic_label(row["name"],
                                   dbq.refinement_of(row["unique_name"]),
                                   always=True)
        return row["name"]

    def _uncounted_note(self, name: str) -> str:
        """Why Forma and its kind find nothing here, or empty."""
        if name.casefold() not in getattr(self, "_uncounted", set()):
            return ""
        return (f"{name} is not counted: Forma, Kuva and the like are not "
                f"inventory.")

    def _near_entries(self, name: str) -> str:
        """" Did you mean: ..." for a name that matched nothing, or empty.

        The same suggestions the name field offers while typing."""
        near = dbq.name_suggestions(name, self._entry_names, limit=5)
        return f" Did you mean: {', '.join(near)}?" if near else ""

    def _add_entry(self):
        name = self.name_edit.text().strip()
        count = self.count_spin.value()
        if not name:
            self.add_feedback.setText("Enter a name first.")
            self.add_feedback.setStyleSheet(f"color: {theme.BAD};")
            return

        entry = self._resolve_entry(name)
        if entry is None:
            if self._ambiguous:
                self.add_feedback.setText(
                    f"{name!r} is both a part and a built item. Choose which "
                    f"one under Save as.")
            elif self._uncounted_note(name):
                self.add_feedback.setText(self._uncounted_note(name))
            else:
                self.add_feedback.setText(
                    f"Nothing called {name!r}.{self._near_entries(name)}")
            self.add_feedback.setStyleSheet(f"color: {theme.BAD};")
            return
        kind, row = entry
        dbq.set_entry_count(self.conn, kind, row, count, "manual")
        label = self._entry_label(kind, row)
        self.add_feedback.setText(
            f"{label}: built x{count}" if kind == "item" else f"{label}: x{count}")
        self.add_feedback.setStyleSheet(f"color: {theme.GOOD};")
        self.name_edit.clear()
        self.holding_label.setText("")
        self._filled_for = None
        self.refresh()

    def _show_holding(self, *_args) -> None:
        """Say what is on record for the typed name, and offer it as the count."""
        name = self.name_edit.text().strip()
        if not name:
            self.holding_label.setText("")
            self.kind_row.setVisible(False)
            self._filled_for = None
            return

        entry = self._resolve_entry(name)
        if entry is None:
            self.holding_label.setText(self._uncounted_note(name))
            self._filled_for = None
            return
        kind, row = entry
        held = dbq.entry_count(self.conn, kind, row)
        self.holding_label.setText(
            f"On record: {self._entry_label(kind, row)} "
            f"({self._KIND_WORDS[kind]}) — {held}. Save writes the number on "
            f"the right, so use the arrow to add one.")
        # Only when the name has changed to a different entry. Filling it on
        # every keystroke would wipe a number the user typed first.
        key = (kind, row["unique_name"])
        if key != self._filled_for:
            self._filled_for = key
            self.count_spin.setValue(held)

    def _clear_source(self):
        source = self.clear_combo.currentText()
        reply = QMessageBox.question(
            self, "Confirm",
            f"Remove every part and item recorded with source {source!r}?")
        if reply != QMessageBox.Yes:
            return
        parts, items = dbq.clear_source(self.conn, source)
        QMessageBox.information(
            self, "Removed", f"Removed {parts} part record(s) and {items} item record(s).")
        self.refresh()

    def _render_sources(self):
        summary = dbq.inventory_summary(self.conn)
        if not summary["parts"] and not summary["items"]:
            self.sources_label.setText("Nothing recorded yet.")
            return
        # Said in words rather than as two numbers. "items · profile: 210
        # kinds, 0 total" is arithmetic that reads as a fault: the profile
        # marks what has been mastered and records no copies, so the zero is
        # the truth and looks like a bug.
        where = {"video": "read from a recording", "profile": "from your "
                 "Warframe profile", "pick": "recorded after a relic run",
                 "manual": "entered by hand", "undo": "restored by an undo",
                 "test": "written by the self-test"}
        lines = []
        for source, (rows, total) in sorted(summary["parts"].items()):
            lines.append(f"{total or 0} part(s) across {rows} kind(s) — "
                         f"{where.get(source, source)}")
        for source, (rows, total) in sorted(summary["items"].items()):
            if total:
                lines.append(f"{total} built item(s) across {rows} kind(s) — "
                             f"{where.get(source, source)}")
            else:
                lines.append(f"{rows} item(s) marked as mastered, none held — "
                             f"{where.get(source, source)}")
        self.sources_label.setText("\n".join(lines))

    # -- public ----------------------------------------------------------
    def refresh(self):
        self._render_progress()
        self._render_duplicates()
        self._render_sources()
