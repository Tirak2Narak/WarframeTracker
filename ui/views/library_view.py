"""Library: search and browse, master-detail, with a separate relic browser.

Item detail shows set composition, which relics drop each piece, ducat values
and vault status. It includes the relic browser: what does this relic contain,
and is it vaulted.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QScrollArea, QSplitter, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets import thumbs
from ui.widgets.filter_chips import ALL, FilterChips
from ui.widgets.tables import simple_table

#: Chip key for "only what I hold". Not a tier, so it is filtered separately.
HELD = "__held__"
from wft.data import build as build_mod, db as dbq

#: Chip keys for the mastery filter. Not categories either, so they are taken
#: out of the chip set before the rest becomes a category clause.
MASTERED = "__mastered__"
UNMASTERED = "__unmastered__"
_MASTERY_KEYS = {MASTERED, UNMASTERED}

#: The mark a mastered row wears, and the blank of roughly its width that every
#: other row wears in its place. The placeholder is the point: a mark on some
#: rows and nothing on the others leaves the names stepping in and out down the
#: left edge, which is harder to read than no mark at all.
MARK = "\u2713\u2002"
_NO_MARK = "\u2003"

#: Whether an item is mastered, as an expression a WHERE clause can use. The
#: subquery finds no row at all for anything never owned, and "never owned" is
#: "not mastered" - so the NULL is answered here rather than left to each
#: caller to forget.
_IS_MASTERED = ("COALESCE((SELECT o.mastered FROM owned_items o "
                "WHERE o.item_unique = i.unique_name), 0)")

_CATEGORY_LABELS = {
    "warframe": "Warframes", "primary": "Primary", "secondary": "Secondary",
    "melee": "Melee", "archwing": "Archwing", "archgun": "Archgun",
    "archmelee": "Archmelee", "necramech": "Necramech",
    "companion": "Companion", "companion_weapon": "Companion Weapon",
    "beast": "Beast",
}

_TIERS = ["Lith", "Meso", "Neo", "Axi", "Requiem"]


def _vault_badge(is_vaulted: bool | None, share: float | None) -> tuple[str, str]:
    if is_vaulted is None:
        return "vault status unknown", theme.TEXT_FAINT
    if is_vaulted:
        return "VAULTED", theme.VAULTED
    if share and share > 0.5:
        return f"{share:.0%} of sources vaulted", theme.WARN
    return "available", theme.GOOD


class LibraryView(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = dbq.connect(cfg.db_path)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("Library")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        # Kept as attributes so the Collection page can host them; the tabs
        # here stay for anything that still opens this view on its own.
        self.items_page = self._items_tab()
        self.relics_page = self._relics_tab()
        tabs.addTab(self.items_page, "Items")
        tabs.addTab(self.relics_page, "Relics")

    def refresh(self):
        """Re-read ownership-dependent fields — called on every tab switch."""
        if self.item_list.currentItem() is not None:
            self._show_item_detail(self.item_list.currentItem())

    # -- items tab -----------------------------------------------------
    def _items_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, theme.px(0.5), 0, 0)
        layout.setSpacing(theme.px(0.7))

        self.item_search = QLineEdit()
        self.item_search.setPlaceholderText("Search items...")
        self.item_search.textChanged.connect(lambda: self._search_timer.start())
        self._search_timer.timeout.connect(self._reload_items)
        layout.addWidget(self.item_search)

        options = [(k, _CATEGORY_LABELS[k]) for k in _CATEGORY_LABELS]
        options += [(MASTERED, "Mastered"), (UNMASTERED, "Not mastered")]
        self.item_chips = FilterChips(options)
        self.item_chips.changed.connect(lambda _s: self._reload_items())
        layout.addWidget(self.item_chips)

        split = QSplitter(Qt.Horizontal)
        layout.addWidget(split, 1)

        self.item_list = QListWidget()
        self.item_list.setObjectName("PickList")
        # Long names are shortened, with the whole name as the tooltip, and
        # the list has no sideways bar: at 20pt in 1600x900 it scrolled by
        # 29px for a name that did not fit.
        self.item_list.setTextElideMode(Qt.ElideRight)
        self.item_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Pictures in the list and a large one in the detail. This is the
        # browsing view - the one place in the application where artwork is
        # what somebody came for rather than a landmark beside a number.
        self.item_list.setIconSize(thumbs.row_size(self.item_list))
        self.item_list.currentItemChanged.connect(self._show_item_detail)
        split.addWidget(self.item_list)

        self.item_detail_scroll = QScrollArea()
        self.item_detail_scroll.setWidgetResizable(True)
        self.item_detail_scroll.setFrameShape(QFrame.NoFrame)
        self.item_detail_host = QWidget()
        self.item_detail_layout = QVBoxLayout(self.item_detail_host)
        self.item_detail_layout.addWidget(self._empty_detail_hint(
            "Select an item to see its set composition, ducat values and "
            "which relics drop each piece."))
        self.item_detail_scroll.setWidget(self.item_detail_host)
        split.addWidget(self.item_detail_scroll)
        # Ems, not pixels, and the list does not take what the detail needs: at
        # 20pt in a 1600x900 window the part table was 742px of the 917 its
        # columns ask for with 420px given to the list.
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([theme.px(18), theme.px(52)])

        self._reload_items()
        return page

    def _empty_detail_hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 1.000em; padding: {theme.px(1.6)}px;")
        label.setAlignment(Qt.AlignTop)
        return label

    def _reload_items(self):
        text = self.item_search.text().strip()
        active = self.item_chips.active()

        # Set progress comes along with the row: a name says whether it is a
        # Prime, it cannot say how close the user is to building it.
        query = ("SELECT i.unique_name, i.name, i.category, i.is_prime, "
                 "(SELECT COUNT(*) FROM parts p "
                 " WHERE p.item_unique = i.unique_name AND p.alias_of IS NULL) AS pieces, "
                 "(SELECT COUNT(*) FROM parts p "
                 " WHERE p.item_unique = i.unique_name AND p.alias_of IS NULL "
                 "   AND (EXISTS (SELECT 1 FROM owned_parts o "
                 "                WHERE o.part_unique = p.unique_name AND o.count > 0) "
                 # A built weapon that is a piece of this one (Lex Prime in
                 # Aklex Prime) lives in owned_items, as owned_count reads.
                 "     OR EXISTS (SELECT 1 FROM owned_items b "
                 "                WHERE b.item_unique = p.unique_name AND b.count > 0 "
                 "                  AND p.unique_name <> i.unique_name))"
                 ") AS held, "
                 "(SELECT count FROM owned_items o WHERE o.item_unique = i.unique_name) AS built, "
                 f"{_IS_MASTERED} AS mastered "
                 "FROM items i WHERE 1=1")
        params: list = []
        # Two questions on one chip row. The categories narrow what kind of
        # thing is listed and the mastery chips narrow which of them are still
        # open, so they are read apart and joined with AND: "Melee" and "Not
        # mastered" together means the melee weapons still to level, not every
        # melee weapon plus everything unmastered.
        categories = {key for key in active if key not in _MASTERY_KEYS} - {ALL}
        if not categories:
            categories = set(build_mod.TRACKED_CATEGORIES)
        placeholders = ",".join("?" * len(categories))
        query += f" AND i.category IN ({placeholders})"
        params.extend(sorted(categories))

        # In the query, never after it. Both chips at once is the same answer
        # as neither, and filtering the result of a LIMIT of 500 would answer
        # with the first five hundred names in the alphabet.
        mastery = active & _MASTERY_KEYS
        if mastery == {MASTERED}:
            query += f" AND {_IS_MASTERED} = 1"
        elif mastery == {UNMASTERED}:
            query += f" AND {_IS_MASTERED} = 0"
        query += " ORDER BY i.name"

        self.item_list.clear()
        # The text is matched here, not in the query: SQL `LIKE` cannot forgive
        # a typo, and the other search boxes use the same `name_matches`. The
        # cap of 500 comes after it, so it still counts matches, not the first
        # five hundred names.
        rows = [row for row in self.conn.execute(query, params)
                if dbq.name_matches(text, [row["name"]])][:500]
        for row in rows:
            # No Prime marker: the word is already in the name, and repeating
            # it costs a column that can carry something unknowable from the
            # name — whether it is built, and how far along the set is.
            # The mark first, because it is the one state that is read down
            # the column rather than along the row: "built" says a copy is in
            # the arsenal, which is neither a rank thirty nor the only way to
            # have earned one - a set sold years ago leaves no copy at all.
            label = (MARK if row["mastered"] else _NO_MARK) + row["name"]
            if row["built"]:
                label = f"{label}   built"
            elif row["pieces"] and row["held"]:
                label = f"{label}   {row['held']}/{row['pieces']}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, row["unique_name"])
            thumbs.decorate_or_space(self.conn, item, "item",
                                     row["unique_name"], self.item_list)
            self.item_list.addItem(item)
        if not rows:
            placeholder = QListWidgetItem("No matches.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.item_list.addItem(placeholder)
        elif self.item_list.currentRow() < 0:
            # Something in the detail pane from the start. Half the page was
            # an instruction to click, on a page where clicking is the only
            # thing to do - so the instruction was the page's whole content
            # until it stopped being needed.
            self.item_list.setCurrentRow(0)

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

    def _show_item_detail(self, current: QListWidgetItem | None, _previous=None):
        self._clear_layout(self.item_detail_layout)
        if current is None or current.data(Qt.UserRole) is None:
            self.item_detail_layout.addWidget(self._empty_detail_hint(
                "Select an item to see its set composition, ducat values and "
                "which relics drop each piece."))
            return

        item_unique = current.data(Qt.UserRole)
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE unique_name=?", (item_unique,)).fetchone()
        if item_row is None:
            return

        header = QLabel(item_row["name"])
        header.setStyleSheet(theme.text("title", weight=600))
        sub = QLabel(_CATEGORY_LABELS.get(item_row["category"], item_row["category"]))
        sub.setStyleSheet(theme.text("body", theme.TEXT_DIM))

        # The picture beside the name, at the size a picture is worth looking
        # at. Everywhere else in the application it is a landmark at two lines
        # tall; here it is the subject.
        crown = QHBoxLayout()
        crown.setSpacing(theme.px(0.9))
        crown.setAlignment(Qt.AlignTop)
        picture = thumbs.icon(self.conn, "item", item_unique)
        if picture is not None:
            side = theme.px(6.0)
            shown = QLabel()
            shown.setPixmap(picture.pixmap(side, side))
            shown.setFixedSize(side, side)
            crown.addWidget(shown, 0, Qt.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(theme.px(0.2))
        words.addWidget(header)
        words.addWidget(sub)
        words.addStretch(1)
        crown.addLayout(words, 1)
        holder = QWidget()
        holder.setLayout(crown)
        self.item_detail_layout.addWidget(holder)

        # The same sentence the item dialog says, from the same function. The
        # two used to be written out separately and had drifted: this one knew
        # about pieces and copies and never mentioned mastery at all.
        from ui.widgets.item_detail import add_keep_box, held_text, state_line

        mastered = dbq.is_mastered(self.conn, item_unique)
        progress = QLabel(state_line(self.conn, item_unique))
        progress.setWordWrap(True)
        progress.setStyleSheet(
            f"color: {theme.GOOD if mastered else theme.TEXT_DIM}; "
            f"font-size: 1.000em; padding-top: {theme.px(0.3)}px;")
        self.item_detail_layout.addWidget(progress)

        # Here, where an item is looked at in the Collection, and not only in
        # the dialog: the Collection page has no tab that opens the dialog.
        # Drawn again on a change, because the mark moves this pane's counts;
        # after the click is handled, since the redraw removes the box itself.
        add_keep_box(self.conn, self.item_detail_layout, item_unique,
                     item_row["name"],
                     lambda c=current: QTimer.singleShot(
                         0, lambda: self._show_item_detail(c)))

        parts = self.conn.execute(
            "SELECT * FROM parts WHERE item_unique=? AND alias_of IS NULL ORDER BY name",
            (item_unique,)).fetchall()

        if not parts:
            self.item_detail_layout.addWidget(self._empty_detail_hint(
                "No set pieces recorded for this item — it may be built from a "
                "blueprint and raw materials only."))
            return

        # The shared table. The Part column carries the name and is the
        # one that wraps; it used to be cut at 199 of 204px at 20pt. Fitted to
        # its rows: the detail pane scrolls, so a table of its own would be a
        # second scrollbar inside it.
        table = simple_table(["Part", "Needed", "Ducats", "You have", "Vault"],
                             len(parts), stretch=0, wrap=True, scroll_x=True)

        missing_total = 0
        for row_index, part in enumerate(parts):
            ducats = dbq.ducats(self.conn, part["unique_name"])
            owned, held = held_text(self.conn, part["unique_name"])
            is_vaulted, share = dbq.vault_state(self.conn, part["unique_name"])
            badge_text, badge_color = _vault_badge(is_vaulted, share)
            short = max(0, part["count_needed"] - owned)
            missing_total += short

            table.setItem(row_index, 0, QTableWidgetItem(part["name"]))
            table.setItem(row_index, 1, QTableWidgetItem(str(part["count_needed"])))
            table.setItem(row_index, 2, QTableWidgetItem(str(ducats) if ducats else "-"))

            # The point of the row is whether this piece is still needed, so the
            # count carries the colour: green when the requirement is met, red
            # with what is outstanding when it is not. Reading "0" against "2"
            # and doing the subtraction is work the table can do.
            have = QTableWidgetItem(f"{held}  (need {short} more)" if short else held)
            have.setForeground(QColor(theme.BAD if short else theme.GOOD))
            table.setItem(row_index, 3, have)

            vault_item = QTableWidgetItem(badge_text)
            vault_item.setForeground(QColor(badge_color))
            table.setItem(row_index, 4, vault_item)

        table.itemSelectionChanged.connect(
            lambda t=table, p=parts: self._show_relics_for_part(p, t))
        self.item_detail_layout.addWidget(table)

        self.item_relics_host = QWidget()
        self.item_relics_layout = QVBoxLayout(self.item_relics_host)
        self.item_relics_layout.setContentsMargins(0, theme.px(0.5), 0, 0)
        self.item_detail_layout.addWidget(self.item_relics_host)
        if parts:
            self._show_relics_for_part(parts, table)
            table.selectRow(0)

    def _show_relics_for_part(self, parts, table: QTableWidget):
        rows = table.selectionModel().selectedRows()
        index = rows[0].row() if rows else 0
        if index >= len(parts):
            return
        part = parts[index]
        self._clear_layout(self.item_relics_layout)

        header = QLabel(f"Relics dropping {part['name']}")
        header.setObjectName("SectionHeader")
        header.setWordWrap(True)
        self.item_relics_layout.addWidget(header)

        from ui.widgets.relic_detail import held_by_refinement

        relics = dbq.relics_dropping(self.conn, part["unique_name"])
        if not relics:
            self.item_relics_layout.addWidget(self._empty_detail_hint("No relic data."))
            return

        # What is on the shelf, first. The list answered "where does this drop"
        # and stopped there, which is the easier half: the piece is missing, so
        # the question is whether it is a farm or a run - and a relic already
        # held is one run. Sorted rather than only labelled, because a shelf of
        # three among sixteen vaulted names is not something to find by reading.
        held = {relic["name"]: held_by_refinement(self.conn, relic["name"])
                for relic in relics}
        ordered = sorted(
            relics,
            key=lambda r: (-sum(held[r["name"]].values()), bool(r["vaulted"]),
                           r["name"]))
        for relic in ordered:
            state = "vaulted" if relic["vaulted"] else "available" if relic["vaulted"] == 0 else "?"
            color = {"vaulted": theme.VAULTED, "available": theme.GOOD,
                     "?": theme.TEXT_FAINT}[state]
            spread = held[relic["name"]]
            text = f"{relic['name']}   ·   {relic['rarity'].title()}   ·   {state}"
            line = QLabel(text)
            if spread:
                # The refinement, not just the count: it decides the odds, and
                # this is the moment the user is choosing which one to open.
                owned = ", ".join(f"{name.title()} x{count}"
                                  for name, count in sorted(spread.items()))
                line.setText(f"{text}   ·   you hold {owned}")
                line.setStyleSheet(f"color: {theme.GOOD}; font-weight: 600;")
            else:
                # Said rather than left blank. A row with nothing after the
                # vault state looks exactly like a row from before this column
                # existed, and the missing line was reported twice for that reason -
                # silence cannot be told apart from a missing feature.
                line.setText(f"{text}   ·   none held")
                line.setStyleSheet(f"color: {color};")
            self.item_relics_layout.addWidget(line)

    # -- relics tab ------------------------------------------------------
    def _relics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, theme.px(0.5), 0, 0)
        layout.setSpacing(theme.px(0.7))

        self.relic_search = QLineEdit()
        self.relic_search.setPlaceholderText("Search relics, e.g. Axi H3...")
        self.relic_search.textChanged.connect(self._reload_relics)
        layout.addWidget(self.relic_search)

        # "Held" beside the tiers. The tab listed the whole catalogue of 772
        # relics and said nothing about which of them are on the shelf, so the
        # one question a player actually brings to it - "do I have any of
        # these" - had no answer anywhere in the tool.
        self.relic_chips = FilterChips([(t, t) for t in _TIERS] + [(HELD, "Held")])
        self.relic_chips.changed.connect(lambda _s: self._reload_relics())
        layout.addWidget(self.relic_chips)

        split = QSplitter(Qt.Horizontal)
        layout.addWidget(split, 1)

        self.relic_list = QListWidget()
        self.relic_list.setObjectName("PickList")
        # Long names are shortened, with the whole name as the tooltip, and
        # the list has no sideways bar: at 20pt in 1600x900 it scrolled by
        # 29px for a name that did not fit.
        self.relic_list.setTextElideMode(Qt.ElideRight)
        self.relic_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.relic_list.currentItemChanged.connect(self._show_relic_detail)
        split.addWidget(self.relic_list)

        self.relic_detail_scroll = QScrollArea()
        self.relic_detail_scroll.setWidgetResizable(True)
        self.relic_detail_scroll.setFrameShape(QFrame.NoFrame)
        self.relic_detail_host = QWidget()
        self.relic_detail_layout = QVBoxLayout(self.relic_detail_host)
        self.relic_detail_layout.addWidget(self._empty_detail_hint(
            "Select a relic to see what it drops and whether it is vaulted."))
        self.relic_detail_scroll.setWidget(self.relic_detail_host)
        split.addWidget(self.relic_detail_scroll)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([theme.px(18), theme.px(52)])

        self._reload_relics()
        return page

    def _reload_relics(self):
        text = self.relic_search.text().strip()
        active = self.relic_chips.active()

        # What is held, summed across refinements, so every row can say so.
        held = {}
        for row in dbq.owned_relics(self.conn):
            held[row["name"]] = (row["count"], row["variants"])

        tiers = {key for key in active if key != HELD}
        only_held = HELD in active

        query = "SELECT unique_name, name, tier, vaulted FROM relics WHERE 1=1"
        params: list = []
        if active != {ALL} and tiers:
            placeholders = ",".join("?" * len(tiers))
            query += f" AND tier IN ({placeholders})"
            params.extend(tiers)
        # Held filtering happens in the query, not after it. Filtering the
        # result of a LIMIT is filtering the first five hundred names in the
        # alphabet: it showed 20 of 32 relics and cut the rest at
        # the letter M, silently.
        if only_held:
            if not held:
                self.relic_list.clear()
                placeholder = QListWidgetItem("No relics recorded yet.")
                placeholder.setFlags(Qt.NoItemFlags)
                self.relic_list.addItem(placeholder)
                return
            placeholders = ",".join("?" * len(held))
            query += f" AND name IN ({placeholders})"
            params.extend(held)
        query += " GROUP BY name ORDER BY name"

        self.relic_list.clear()
        # Text matched here, before the cap, with the same forgiving
        # `name_matches` as the other search boxes (see `_reload_items`).
        rows = [row for row in self.conn.execute(query, params)
                if dbq.name_matches(text, [row["name"]])][:500]
        from ui.widgets.relic_detail import held_by_refinement

        for row in rows:
            marker = " (vaulted)" if row["vaulted"] else ""
            count, variants = held.get(row["name"], (0, 0))
            if count:
                # Which refinement, not only how many. Twelve Intact and three
                # Radiant are not fifteen of one thing - they are two sets of
                # odds, and the list named neither.
                spread = held_by_refinement(self.conn, row["name"])
                detail = ", ".join(f"{name.title()} ×{n}"
                                   for name, n in sorted(spread.items()))
                marker = (f"   {detail}" if detail else f"   ×{count}") + marker
            # Two different relics can print the same name. Saying so is the
            # only honest thing to do with a count that covers both.
            if variants > 1:
                marker += f"   ({variants} different relics share this name)"
            item = QListWidgetItem(f"{row['name']}{marker}")
            item.setData(Qt.UserRole, row["unique_name"])
            self.relic_list.addItem(item)
        if not rows:
            placeholder = QListWidgetItem("No matches.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.relic_list.addItem(placeholder)

    def _show_relic_detail(self, current: QListWidgetItem | None, _previous=None):
        """The same detail the relic window shows, built into the panel.

        This used to be its own smaller version: name, vault status, and the
        rewards with their rarity. It never said which refinement the relic
        was - the fact that decides every drop chance on it, so a shelf of
        Radiant relics was described in Intact odds by omission.
        """
        from ui.widgets import relic_detail

        self._clear_layout(self.relic_detail_layout)
        if current is None or current.data(Qt.UserRole) is None:
            self.relic_detail_layout.addWidget(self._empty_detail_hint(
                "Select a relic to see what it drops and whether it is vaulted."))
            return

        row = self.conn.execute("SELECT name FROM relics WHERE unique_name=?",
                                (current.data(Qt.UserRole),)).fetchone()
        if row is None:
            return
        relic_detail.fill(self.relic_detail_layout, self.conn, row["name"])
