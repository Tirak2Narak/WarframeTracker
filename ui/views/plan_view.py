"""What to do next: build it, finish it, or farm it before it is gone.

The other pages answer questions about a thing you name. This one answers the
question you actually arrive with - what should I do with this collection - and
it answers it in the order a reward is ranked in: vault status
first, then how close a set is, then ducats. Platinum is deliberately absent.

Four sections, in the order they deserve attention:

    Ready to build      every piece held, nothing built - act now, no farming
    One piece away      the shortest path to a new frame or weapon
    Vaulted and short   still obtainable only from relics already held
    Ducat fodder        duplicates worth trading in

"Vaulted" needs care. A vaulted relic is out of the drop tables, but the parts
it drops are not out of the game - they are still tradeable, and any relic
already in the inventory still opens. So this never says "lost", it says the
supply is finite, which is what makes it worth doing sooner than the rest.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QLineEdit,
    QFrame, QHBoxLayout, QLabel, QScrollArea,
    QMessageBox, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets import progress_cell, thumbs
from ui.widgets.diamond import Diamond
from ui.widgets.filter_chips import ALL, FilterChips
from ui.widgets.section import Section
from ui.widgets.tables import (
    Sort, fit_height, order_by, simple_table as _table, sortable,
)

#: Chip keys for the missing-pieces table. Three views of one list, which is
#: what they always were - they had three sections, three caps and three sorts
#: before, and a row could appear in all of them.
FINISHES = "__finishes__"
VAULTED = "__vaulted__"
MASTERED = "__mastered__"
#: The other half of MASTERED, and the one that gets asked for. Two thirds of
#: this list is sets whose mastery is already banked - a Carrier that was built
#: years ago still owes its blueprint - and they are the rows nobody is
#: shopping for. Showing them is a filter; hiding them needed one too.
WANTED = "__wanted__"
from wft.data import db as dbq
from wft.data.survey import set_survey

#: Sets this close to complete are worth a section of their own. Two missing
#: pieces is still a project; one is an evening.
NEARLY_DONE = 1

#: The missing-pieces table scrolls rather than growing without bound. Tall
#: enough that filtering is a refinement, not the only way to see anything. In
#: rows: it was 420px, which is seven rows at 14pt and four at 20pt.
MISSING_TABLE_ROWS = 7

#: Multi-piece sets sort above single blueprints. Not because a blueprint is
#: worth less - resources are not tracked and so are not a criterion, and a held
#: blueprint is as buildable as a finished set - but because a four-piece set
#: represents farming that is now done, and that is the part worth seeing first.
REAL_SET = 2


class PlanView(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = dbq.connect(cfg.db_path)
        dbq.assert_usable(self.conn)
        #: The order each table is in, kept across the rebuild the page does
        #: after every import.
        self._order = {"missing": {"column": 2, "descending": False},
                       "ready": {"column": 0, "descending": False}}
        # Built empty. `MainWindow.select` refreshes every page on the way in,
        # so filling it here meant the most expensive page in the application
        # was surveyed twice before anyone saw it once.
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("Plan")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        subtitle = QLabel(
            "Ranked by vault status, then how close a set is, then ducats.")
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
        while self.body.count():
            child = self.body.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    # -- data -------------------------------------------------------------
    def _make_clickable(self, table, entries) -> None:
        """Open the item's detail when a row is chosen.

        Every section is a list of items, and the question after every one of
        them is the same: what is still missing and what drops it. That answer
        already exists as `ItemDetail` and was only reachable from the
        Inventory page, so on the page that names the gaps there was nothing to
        click, and nobody could find that out except by clicking.

        Keyed on the row's position rather than its text, because two sections
        can name the same item and the text is not an identifier.
        """
        from ui.widgets.item_detail import ItemDetail

        table.setCursor(Qt.PointingHandCursor)
        table.setToolTip("Click an item to see what it needs and what drops it")

        def open_row(row: int, _column: int = 0):
            if 0 <= row < len(entries):
                entry = entries[row]
                if entry is None:
                    return
                dialog = ItemDetail(self.conn, entry["unique"], entry["name"], self)
                dialog.exec()
                # The "keep one" mark moves what is short.
                if dialog.kept_changed:
                    self.refresh()

        table.cellClicked.connect(open_row)

    def _survey(self):
        """Every tracked set, with what is held and what is missing.

        The pass itself lives in `wft.data.survey.set_survey`, shared with the
        Trading lists; this page only asks for it.
        """
        return set_survey(self.conn)

    def _empty_text(self) -> str:
        """Why there is nothing to plan, and the page that fixes it.

        Without game data nothing can be named at all, which is Setup's job;
        with it, the page is empty because nothing has been recorded yet.
        """
        from wft import readiness

        try:
            have_data = readiness.data_check(self.cfg).ok
        except Exception:  # noqa: BLE001 - an empty-state text must not fail
            have_data = True
        if not have_data:
            return ("Nothing to plan yet: the game data is not downloaded. Open "
                    "Setup (bottom left) and press Download game data.")
        return ("Nothing to plan yet. Record your inventory on the Import page, "
                "or add items by hand under Collection → Add and correct.")

    # -- rendering --------------------------------------------------------
    def refresh(self):
        self._clear()
        survey = self._survey()

        if not survey:
            hint = QLabel(self._empty_text())
            hint.setWordWrap(True)
            hint.setObjectName("ViewSubtitle")
            self.body.addWidget(hint)
            self.body.addStretch(1)
            return

        # Mastered items are done. Farming the last piece of a set whose
        # mastery is already banked is work for nothing, and showing it there
        # crowds out the sets that still owe points - which is the whole
        # question this page answers.
        relic_cover = self._relic_cover()

        # Three blocks, not eight. "One piece away", "Vaulted and incomplete"
        # and "Mastered, nearly complete" were three tables of the same rows
        # under three headings - filters of the missing-pieces list, each with
        # its own cap, its own sort and its own scroll. Filters they are, and
        # filters they now look like: one table, chips above it.
        #
        # What is left reads as three questions in the order they get asked:
        # what do I open, what am I missing, and what is done with.
        self._mastery_header()
        # Every set, not just the unmastered ones. A full set is something to
        # do rather than something to farm, and passing the farming list here
        # hid Orthos Prime - four pieces of four, nothing built, and the
        # section read "0 ready to build" because its mastery was banked.
        # Mastered is exactly when the question gets interesting: build it, or
        # sell the pieces.
        self._ready_section(survey)
        self._missing_section(survey, relic_cover)
        # Spares, mastered sets and doubles used to sit under here. They are
        # the Ducats page now: this page is what the collection still needs,
        # and that one is what to do with what it does not.
        self.body.addStretch(1)


    def _mastery_header(self):
        """Where the account stands, in the number the user plays for."""
        mastered, trackable = dbq.mastered_count(self.conn)
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(theme.px(1.1), theme.px(0.8), theme.px(1.1), theme.px(0.8))

        top = QHBoxLayout()
        top.setSpacing(2)
        top.addWidget(Diamond(theme.ACCENT))
        heading = QLabel(f"Mastered — {mastered} of {trackable} tracked items")
        heading.setStyleSheet(theme.section_text(theme.ACCENT))
        top.addWidget(heading)
        top.addStretch(1)
        layout.addLayout(top)

        note = QLabel(
            "Mastery is kept when an item is sold, so this counts what has "
            "been levelled rather than what is held. The farming sections "
            "below leave mastered sets out; the missing-pieces list keeps "
            "them, dimmed.")
        note.setObjectName("ViewSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.body.addWidget(frame)

    def _relic_cover(self):
        """part_unique -> how many held relics drop it.

        One query rather than one per part: the missing list runs to hundreds
        of rows and `relics_dropping` alone is a query each. Aliases are folded
        in because a part carries two unique names - relics drop the blueprint,
        the inventory holds the component - and counting only one reports zero
        for parts that are covered.
        """
        cover: dict[str, int] = {}
        for row in self.conn.execute(
                "SELECT rr.part_unique AS part, SUM(o.count) AS held "
                "FROM owned_relics o "
                "JOIN relic_rewards rr ON rr.relic_unique = o.relic_unique "
                "GROUP BY rr.part_unique"):
            for member in dbq.alias_group(self.conn, row["part"]):
                cover[member] = cover.get(member, 0) + int(row["held"] or 0)
        return cover

    def _missing_section(self, survey, relic_cover):
        """Every piece still needed, named, with what it takes to get it.

        This is the question the page exists for and the sections below only
        answer obliquely: they rank whole sets, and a set is not a thing you
        can go and get. A part is. So this lists parts, one row each, with no
        cap - a hundred rows the user can filter beats ten the tool chose.

        The last column is the actionable one: a relic already in the
        inventory means the piece is one run away rather than one farm away.
        """
        rows = []
        for entry in survey:
            for name, short, vaulted in entry["missing"]:
                rows.append((name, entry, short, vaulted))

        section = self._section_widget(
            f"Pieces you still need — {len(rows)}",
            "Every part missing from a set you have started. Sorted by how "
            "close the set is, so the top of the list is what finishes "
            "something. Sets whose mastery is already banked are dimmed and "
            "sort last at equal distance. Filters combine — two chips means "
            "both, so \"Vaulted\" plus \"Finishes a set\" is the short list "
            "worth acting on.",
            theme.ACCENT, start_open=True)
        layout = section.body
        heading = section.toggle

        if not rows:
            self._empty(layout, "Every set you have started is complete.")
            return

        search = QLineEdit()
        search.setPlaceholderText("Filter by item or part name")
        layout.addWidget(search)

        chips = FilterChips([
            (FINISHES, "Finishes a set"),
            (WANTED, "Mastery still open"),
            (VAULTED, "Vaulted"),
            (MASTERED, "Mastery already banked"),
        ], all_label="Everything")
        layout.addWidget(chips)

        table = _table(["Part", "Belongs to", "Set", "Need", "Vault",
                        "Relics held"], len(rows), stretch=0, wrap=True,
                       scroll_x=True)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # The set's picture beside the set's name. Not beside the part: a
        # component's own texture is usually a generic blueprint, and forty of
        # those in a column is noise where the set's icon is a landmark.
        table.setIconSize(thumbs.row_size(table))
        table.verticalHeader().setDefaultSectionSize(thumbs.row_height(table))
        table.setItemDelegateForColumn(2, progress_cell.ProgressDelegate(table))
        columns = {
            0: Sort(lambda r: r[0].casefold()),
            1: Sort(lambda r: r[1]["name"].casefold()),
            # The bar reads as how close the set is, so that is what it
            # sorts on: the fewest pieces still owed first, which is the
            # order this page opens in and the reason it exists.
            2: Sort(lambda r: sum(m[1] for m in r[1]["missing"])),
            3: Sort(lambda r: r[2], descending_first=True),
            # Unknown vault state (None) is shown as "available" and is
            # tie-broken as farmable below, so it sorts as farmable here too;
            # the raw None/bool mix made the sort raise TypeError.
            4: Sort(lambda r: bool(r[3]), descending_first=True),
            5: Sort(lambda r: self._cover_for(relic_cover, r[1]["unique"],
                                             r[0]), descending_first=True),
        }
        owners = [None] * len(rows)

        def fill():
            # Nearly-done sets first, then vaulted ahead of farmable at
            # equal distance - the order the section was written to have,
            # now reachable again by clicking the column it belongs to.
            order_by(rows, columns, self._order["missing"],
                     tiebreak=lambda r: (bool(r[1]["mastered"]), not r[3],
                                         r[1]["name"].casefold(), r[0]))
            owners[:] = [entry for _part, entry, _s, _v in rows]
            for i, (part_name, entry, short, vaulted) in enumerate(rows):
                part_cell = QTableWidgetItem(part_name)
                owner = QTableWidgetItem(entry["name"])
                if entry["mastered"]:
                    # Still listed - the set sells whole - but not competing for
                    # attention with one that still owes mastery points.
                    part_cell.setForeground(QColor(theme.TEXT_FAINT))
                    owner.setForeground(QColor(theme.TEXT_FAINT))
                table.setItem(i, 0, part_cell)
                thumbs.decorate(self.conn, owner, "item", entry["unique"])
                table.setItem(i, 1, owner)
                progress = QTableWidgetItem()
                progress_cell.set_progress(progress, entry["held"], entry["total"])
                table.setItem(i, 2, progress)
                table.setItem(i, 3, QTableWidgetItem(f"x{short}"))
                mark = QTableWidgetItem("vaulted" if vaulted else "available")
                mark.setForeground(QColor(theme.VAULTED if vaulted
                                          else theme.TEXT_DIM))
                table.setItem(i, 4, mark)
                held = self._cover_for(relic_cover, entry["unique"], part_name)
                cell = QTableWidgetItem(str(held) if held else "—")
                if held:
                    cell.setForeground(QColor(theme.GOOD))
                table.setItem(i, 5, cell)

        fill()

        progress_cell.fit_column(table, 2)

        # A fixed height would either cut the list off or make the page a mile
        # long; this is the one table that scrolls on its own. Measured again
        # first: the height it was built with predates being told how tall a
        # row with a picture in it is.
        fit_height(table, max_rows=MISSING_TABLE_ROWS)

        def apply_filter(*_args):
            needle = search.text().strip().casefold()
            active = chips.active()
            shown = 0
            for row, (part_name, entry, _short, vaulted) in enumerate(rows):
                keep = dbq.name_matches(needle, [
                    table.item(row, col).text() for col in (0, 1)])
                if keep and active != {ALL}:
                    # AND across the chips. It was OR, on the argument that
                    # "Vaulted" plus "Finishes a set" reads as either - but a
                    # union is always at least as wide as its widest member,
                    # so the narrow chip vanishes into the broad one and looks
                    # broken. Measured: "Finishes a set" alone is 12 rows,
                    # "Mastery still open" is 91, and the two together were 91
                    # - the first chip did nothing at all.
                    #
                    # Narrowing is also what the combination is for. "Vaulted
                    # and one piece short" is the list worth acting on; either
                    # of them alone is most of the page.
                    matches = set()
                    if sum(m[1] for m in entry["missing"]) == 1:
                        matches.add(FINISHES)
                    if vaulted:
                        matches.add(VAULTED)
                    if entry["mastered"]:
                        matches.add(MASTERED)
                    else:
                        matches.add(WANTED)
                    keep = active <= matches
                table.setRowHidden(row, not keep)
                shown += keep
            heading.setText(f"Pieces you still need — {shown} of {len(rows)}"
                            if shown != len(rows)
                            else f"Pieces you still need — {len(rows)}")

        search.textChanged.connect(apply_filter)
        chips.changed.connect(apply_filter)
        # The filter hides rows by index, so it has to run again after every
        # reordering or it would go on hiding the rows that used to be there.
        sortable(table, columns, self._order["missing"],
                 lambda: (fill(), apply_filter()))
        self._make_clickable(table, owners)
        layout.addWidget(table)

    def _cover_for(self, relic_cover, item_unique, part_name):
        """Held relics dropping the named part of the named set."""
        row = self.conn.execute(
            "SELECT unique_name FROM parts WHERE item_unique=? AND name=? "
            "AND alias_of IS NULL", (item_unique, part_name)).fetchone()
        return relic_cover.get(row["unique_name"], 0) if row else 0

    def _section(self, title: str, note: str, colour: str,
                 start_open: bool = True) -> QVBoxLayout:
        return self._section_widget(title, note, colour, start_open).body

    def _section_widget(self, title: str, note: str, colour: str,
                        start_open: bool = True) -> Section:
        """The section itself, for callers that need to retitle it later."""
        section = Section(title, note, colour, start_open)
        self.body.addWidget(section)
        return section

    def _empty(self, layout: QVBoxLayout, text: str):
        label = QLabel(text)
        label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        label.setWordWrap(True)
        layout.addWidget(label)

    def _ready_section(self, survey):
        """Sets with every piece in hand and nothing built yet.

        Its own section, above the missing list rather than folded away at the
        bottom of "Done with". This is the only part of the page that is an
        action instead of a plan: the pieces are already on the shelf and the
        foundry is one click away. Buried under a collapsed heading it was
        read as a footnote about ducats.
        """
        # `built` is deliberately not a condition. A built copy does not
        # consume the pieces of a second one: a player can have Gauss Prime at rank
        # 30 *and* a full set of its parts on the shelf, and the section that
        # exists to say "these pieces are a complete set" was hiding it
        # because one had already been made.
        ready = [s for s in survey
                 if s["held"] >= s["total"] and not s["mastered"]]
        if not ready:
            return

        layout = self._section(
            f"Ready to build — {len(ready)} complete set(s)",
            "Every piece held, nothing built, and the mastery still owed. "
            "Building these is the shortest way to move the number at the top "
            "of the page.",
            theme.GOOD)
        # The last column, as in "Sell set". The foundry takes the pieces the
        # moment the build starts; the import cannot see the result until it
        # is finished, levelled and photographed, which is days away.
        table = _table(["Set", "Category", "Pieces", "Built it?"], len(ready))
        columns = {
            0: Sort(lambda entry: entry["name"].casefold()),
            1: Sort(lambda entry: (entry["category"] or "").casefold()),
            2: Sort(lambda entry: entry["total"], descending_first=True),
        }

        def fill():
            order_by(ready, columns, self._order["ready"])
            for index, entry in enumerate(ready):
                table.setItem(index, 0, QTableWidgetItem(entry["name"]))
                table.setItem(index, 1,
                              QTableWidgetItem(entry["category"] or "—"))
                table.setItem(index, 2, QTableWidgetItem(f"{entry['total']}"))
                made = QTableWidgetItem("Built it")
                made.setForeground(QColor(theme.ACCENT))
                made.setToolTip("Take the pieces off the shelf and put a copy "
                                "in the arsenal.")
                table.setItem(index, 3, made)

        fill()
        sortable(table, columns, self._order["ready"], fill)
        def open_ready(row: int, column: int) -> None:
            if column == 3:
                self._built_it(ready[row])
                return
            dialog = ItemDetail(self.conn, ready[row]["unique"],
                                ready[row]["name"], self)
            dialog.exec()
            # A copy kept for oneself changes what is spare.
            if dialog.kept_changed:
                self.refresh()

        table.cellClicked.connect(open_ready)
        table.setToolTip("Click a set to see its pieces, or a heading to "
                         "reorder them")
        layout.addWidget(table)

    def _built_it(self, entry) -> None:
        """One set went into the foundry.

        Written when the build starts rather than when a scan finds the
        result: the pieces are consumed at that moment, and waiting for a
        recording means the page keeps offering a set that is already gone.
        """
        pieces = dbq.pieces_of_one_set(self.conn, entry["unique"], 1)
        if not pieces:
            QMessageBox.information(
                self, entry["name"],
                "No pieces of this set are recorded, so there is nothing for "
                "the foundry to take.")
            return
        listing = "\n".join(
            f"  {name}: {now} → {after}" for _kind, _unique, name, now, after
            in pieces)
        answer = QMessageBox.question(
            self, f"{entry['name']} — built it?",
            f"The foundry takes these:\n\n{listing}\n\n"
            f"{entry['name']} goes into the arsenal. Mastery comes later — a "
            f"fresh build is rank 0 and the profile import notices when it "
            f"reaches 30.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        dbq.build_one_set(self.conn, entry["unique"], 1, "manual")
        self.refresh()

