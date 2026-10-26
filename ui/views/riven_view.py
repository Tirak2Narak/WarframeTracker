"""Whether rivens for a weapon are worth anything, before the roll is rolled.

The question this page answers is the one asked while a veiled riven sits in
the inventory: is there a market for this weapon's rivens at all, or does the
market treat them as dissolution fodder? The roll dominates any single price
and no feed can see the roll — so everything here is a ballpark figure, never
a price tag, and the page says so where the numbers are.

The data is DE's own weekly file of real trades (see wft/riven/prices.py for
the feed's habits). Only the current week is ever served, so the history the
detail dialog shows grows locally: a week the tool never saw is a hole nothing
can refill. The fetch is one attempt per program run — the file changes once
a week, and on a Monday the edge may serve the old week for hours, so there
is deliberately no refresh button here: it could only re-ask a question whose
answer is cached for a day.

Most rows are rumours. In a normal week two thirds of the weapons have two
trades or fewer behind their numbers, so the table pools the trade count
across every stored week and dims the rows where even that pool is thin —
the honest reading of "median 2900" backed by a single trade is "somebody,
once".
"""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QLabel, QLineEdit, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets.dialog_size import fit_dialog
from ui.widgets.filter_chips import ALL, FilterChips
from ui.widgets.section import Section
from ui.widgets.tables import (
    Sort, fit_height, order_by, simple_table as _table, sortable,
)
from ui.workers.task import TaskWorker
from wft.data import db as dbq
from wft.riven import prices as prices_mod

#: Above this many rows the weapons table gets its own scrollbar. Four hundred
#: weapons are a list to search, not a page to scroll. In rows: it was 520px,
#: fifteen rows at 14pt and nine at 20pt.
WEAPON_TABLE_ROWS = 15

#: Fetches still in flight, held here rather than on the page — a QThread
#: parented to a widget is destroyed with it, and destroying a running
#: QThread aborts the process. Same wiring as the fissure board's.
_in_flight: set = set()


def wait_for_fetches(ms_each: int = 5000) -> None:
    """Let a fetch still in flight finish before the interpreter goes.

    Called from the quit path and from the self-test, which never enters the
    event loop. Five seconds a worker against a twenty-second request timeout
    covers every fetch that is going to finish at all.
    """
    for worker in list(_in_flight):
        worker.wait(ms_each)


def _num(value) -> str:
    """A platinum figure, or the honest dash for one that does not exist."""
    if value is None:
        return "—"
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _day(iso: date) -> str:
    return f"{iso.strftime('%b')} {iso.day}"


def _short_type(item_type: str) -> str:
    """'Rifle Riven Mod' is a 'Rifle' in a table that says Riven at the top."""
    return item_type.removesuffix(" Riven Mod")


def _worth(value) -> float:
    """A price as a number a column can be sorted on.

    Not traded this week is not the same as traded for nothing, but a column
    has to put it somewhere. It goes to the bottom of the expensive end, which
    is where it belongs: an empty week is not a cheap riven, it is no answer,
    and no answer is not what anybody is scrolling to the top for.
    """
    return float("-inf") if value is None else float(value)


class RivenView(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = dbq.connect(cfg.db_path)
        dbq.assert_usable(self.conn)
        self.error: str | None = None
        self._worker = None
        self._board_table = None
        self._board_rows: list = []
        #: The order of each table, kept across the rebuild the page does
        #: whenever a week is fetched.
        self._order = {"weapons": {"column": 3, "descending": True},
                       "veiled": {"column": 0, "descending": False}}
        self._build_ui()
        # No fetch here: `MainWindow.select` refreshes the page on the way in,
        # which is the first moment anybody is looking.

    # -- plumbing ---------------------------------------------------------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("Rivens")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        subtitle = QLabel(
            "What rivens for a weapon actually trade for — DE's weekly "
            "numbers for PC, real trades rather than listings. The roll "
            "dominates any single price; these are ballpark figures.")
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
        # Taken apart *now*, not on the next event-loop beat: this page
        # rebuilds when the fetch answers, possibly under the user's hands —
        # the fissure page documents the click that taught us this.
        while self.body.count():
            child = self.body.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def refresh(self):
        """Rebuild from the database, then see whether this week is missing.

        The page never waits for the network: everything on it comes from
        stored weeks, and the fetch — when one happens at all — repaints on
        arrival. `refresh_if_stale` holds the whole decision (already stored,
        already attempted this run) so the view cannot grow a second opinion
        about staleness.
        """
        self._render()
        self._fetch()

    def _fetch(self):
        if self._worker is not None and self._worker.isRunning():
            return
        worker = TaskWorker(
            lambda: prices_mod.refresh_if_stale(self.cfg, self.cfg.db_path))
        _in_flight.add(worker)
        worker.finished.connect(lambda w=worker: _in_flight.discard(w))
        worker.finished_ok.connect(self._week_answered)
        worker.failed.connect(self._week_failed)
        self._worker = worker
        worker.start()

    def _week_answered(self, week):
        # None means nothing needed doing — but on an empty database the page
        # is showing "Reading the week…" and has to move on to the truth.
        self.error = None
        if week is not None or not prices_mod.weeks(self.conn):
            self._render()

    def _week_failed(self, message: str):
        self.error = str(message)
        self._render()

    # -- rendering --------------------------------------------------------
    def _render(self):
        self._clear()
        self._board_table = None
        self._board_rows = []

        stored = prices_mod.weeks(self.conn)
        if stored:
            self.body.addWidget(self._head(stored))
            if self.error:
                # The stored weeks carry the page; the failed fetch is one
                # line, not a state. No "will retry" promise: the next
                # attempt is the next program run.
                note = QLabel(f"This week could not be fetched: {self.error}")
                note.setWordWrap(True)
                note.setStyleSheet(f"color: {theme.WARN};")
                self.body.addWidget(note)
            self._weapons_section()
            self._veiled_section()
        else:
            self._empty_state()
        self.body.addStretch(1)

    def _head(self, stored: list[str]) -> QLabel:
        monday = date.fromisoformat(stored[0])
        first, last = monday - timedelta(days=7), monday - timedelta(days=1)
        count = len(stored)
        label = QLabel(
            f"Week of {_day(first)} – {_day(last)}, published {_day(monday)}"
            f" · {count} week{'s' if count != 1 else ''} collected")
        label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        return label

    def _empty_state(self):
        text = QLabel()
        text.setWordWrap(True)
        if self.error:
            # One attempt per program run, so promising a retry in a couple
            # of minutes would be a lie the fissure page is allowed to tell
            # and this one is not.
            text.setText(
                f"The week's riven prices could not be fetched: {self.error}\n\n"
                f"The next attempt happens on the next program start. The "
                f"data changes once a week, so nothing is missed by waiting.")
            text.setStyleSheet(f"color: {theme.WARN};")
        elif self._worker is not None and self._worker.isRunning():
            text.setText("Reading the week…")
            text.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        else:
            text.setText("No data yet — the first successful weekly fetch "
                         "fills this page.")
            text.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        self.body.addWidget(text)

    def _weapons_section(self):
        rows = [row for row in prices_mod.board(self.conn)]
        section = Section("Weapons", "", theme.ACCENT)
        self.body.addWidget(section)
        layout = section.body
        if not rows:
            note = QLabel("The stored weeks hold no weapon rows.")
            note.setStyleSheet(f"color: {theme.TEXT_FAINT};")
            layout.addWidget(note)
            return

        search = QLineEdit()
        search.setPlaceholderText("Filter by weapon name")
        layout.addWidget(search)

        seen = sorted({row["item_type"] for row in rows})
        chips = FilterChips([(t, _short_type(t)) for t in seen],
                            all_label="Every type")
        layout.addWidget(chips)

        table = _table(["Weapon", "Type", "Median unrolled", "Median rolled",
                        "Max rolled", "Trades"], len(rows), stretch=0,
                        wrap=True, scroll_x=True)
        columns = {
            0: Sort(lambda r: r["weapon"].casefold()),
            1: Sort(lambda r: r["item_type"]),
            2: Sort(lambda r: _worth(r["median_unrolled"]),
                    descending_first=True),
            3: Sort(lambda r: _worth(r["median_rolled"]),
                    descending_first=True),
            4: Sort(lambda r: _worth(r["max_rolled"]), descending_first=True),
            # The column reads "this week / everything stored", and the second
            # number is the one that says how much a price is worth trusting.
            5: Sort(lambda r: r["pop_pooled"] or 0, descending_first=True),
        }

        def fill():
            # Most asked-for first: what a rolled riven fetches, best guess.
            order_by(rows, columns, self._order["weapons"],
                     tiebreak=lambda r: (r["weapon"].casefold(),
                                         r["item_type"]))
            for index, row in enumerate(rows):
                faint = (row["pop_pooled"] or 0) <= 2
                week_pop = (row["pop_unrolled"] or 0) + (row["pop_rolled"] or 0)
                cells = [
                    QTableWidgetItem(row["weapon"]),
                    QTableWidgetItem(_short_type(row["item_type"])),
                    QTableWidgetItem(_num(row["median_unrolled"])),
                    QTableWidgetItem(_num(row["median_rolled"])),
                    QTableWidgetItem(_num(row["max_rolled"])),
                    QTableWidgetItem(f"{week_pop} / {row['pop_pooled'] or 0}"),
                ]
                for column, cell in enumerate(cells):
                    if faint:
                        cell.setForeground(QColor(theme.TEXT_FAINT))
                    table.setItem(index, column, cell)

        fill()
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setToolTip("Click a weapon for its week-by-week history, or a "
                         "heading to reorder the list")
        table.cellClicked.connect(
            lambda row, _column: self._open_detail(rows[row]))
        # `simple_table` sizes itself to its rows; four hundred weapons must
        # not make the page four hundred rows long.
        fit_height(table, max_rows=WEAPON_TABLE_ROWS)
        layout.addWidget(table)
        self._board_table = table
        self._board_rows = rows

        footnote = QLabel("Faint rows have two trades or fewer on record — "
                          "a rumour, not a price. Trades reads week / all "
                          "stored weeks.")
        footnote.setWordWrap(True)
        footnote.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        layout.addWidget(footnote)

        def apply_filter(*_args):
            needle = search.text().strip().casefold()
            active = chips.active()
            shown = 0
            for row_index, entry in enumerate(rows):
                keep = dbq.name_matches(needle, [entry["weapon"]])
                if keep and active != {ALL}:
                    keep = entry["item_type"] in active
                table.setRowHidden(row_index, not keep)
                shown += keep
            section.toggle.setText(
                f"Weapons — {shown} of {len(rows)}" if shown != len(rows)
                else f"Weapons — {len(rows)}")

        search.textChanged.connect(apply_filter)
        chips.changed.connect(apply_filter)
        # The search and the chips hide rows by index, so what is hidden has
        # to be decided again every time the rows move under them.
        sortable(table, columns, self._order["weapons"],
                 lambda: (fill(), apply_filter()))
        apply_filter()

    def _veiled_section(self):
        rows = prices_mod.veiled(self.conn)
        if not rows:
            return
        section = Section(
            "Veiled", "What an unopened riven of each type went for this "
            "week — the baseline under every unveiled number above.",
            theme.ACCENT, start_open=False)
        self.body.addWidget(section)
        rows = list(rows)
        table = _table(["Type", "Avg", "Median", "Min–Max", "Trades"],
                       len(rows), stretch=0)
        columns = {
            0: Sort(lambda r: r["item_type"]),
            1: Sort(lambda r: _worth(r["avg"]), descending_first=True),
            2: Sort(lambda r: _worth(r["median"]), descending_first=True),
            # The span is read from its cheap end, which is the number a
            # veiled riven of that type can actually be had for.
            3: Sort(lambda r: _worth(r["price_min"]), descending_first=True),
            4: Sort(lambda r: r["pop"] or 0, descending_first=True),
        }

        def fill():
            order_by(rows, columns, self._order["veiled"])
            for index, row in enumerate(rows):
                table.setItem(index, 0,
                              QTableWidgetItem(_short_type(row["item_type"])))
                table.setItem(index, 1, QTableWidgetItem(_num(row["avg"])))
                table.setItem(index, 2, QTableWidgetItem(_num(row["median"])))
                table.setItem(index, 3, QTableWidgetItem(
                    f"{_num(row['price_min'])} – {_num(row['price_max'])}"))
                table.setItem(index, 4, QTableWidgetItem(_num(row["pop"])))

        fill()
        sortable(table, columns, self._order["veiled"], fill)
        section.body.addWidget(table)

    def _open_detail(self, row) -> None:
        RivenDetail(self.conn, row["item_type"], row["weapon"], self).exec()


class RivenDetail(QDialog):
    """One weapon's rivens, week by week — as far back as the tool was here.

    The history is local by nature: DE serve only the current week, so this
    table is exactly the weeks this tool was running to catch. That is said
    on the dialog rather than left to be discovered.
    """

    def __init__(self, connection, item_type: str, weapon: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(weapon)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.7))

        heading = QLabel(weapon)
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)

        summary = QLabel(f"{_short_type(item_type)} riven  ·  history grows "
                         f"locally — a week the tool never saw stays a gap")
        summary.setObjectName("ViewSubtitle")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        history = prices_mod.history(connection, item_type, weapon)
        by_week: dict[str, dict[int, object]] = {}
        for row in history:
            by_week.setdefault(row["week"], {})[int(row["rerolled"])] = row

        # One row per week, both halves of it in hand, so a column can be
        # sorted on a figure that lives in either.
        weeks = [(week, by_week[week].get(0), by_week[week].get(1))
                 for week in by_week]

        def figure(row, field):
            return _worth(row[field] if row is not None else None)

        table = _table(["Week", "Median unrolled", "Trades unrolled",
                        "Median rolled", "Avg rolled", "Min–Max rolled",
                        "Stddev rolled", "Trades rolled"],
                       len(weeks), stretch=0, scroll_x=True)
        columns = {
            0: Sort(lambda w: w[0], descending_first=True),
            1: Sort(lambda w: figure(w[1], "median"), descending_first=True),
            2: Sort(lambda w: figure(w[1], "pop"), descending_first=True),
            3: Sort(lambda w: figure(w[2], "median"), descending_first=True),
            4: Sort(lambda w: figure(w[2], "avg"), descending_first=True),
            5: Sort(lambda w: figure(w[2], "price_min"),
                    descending_first=True),
            6: Sort(lambda w: figure(w[2], "stddev"), descending_first=True),
            7: Sort(lambda w: figure(w[2], "pop"), descending_first=True),
        }
        #: Newest week first. This history is short by nature - it is only the
        #: weeks the tool was running to catch - so the top of it is the price
        #: that is current, and that is what the dialog is opened for.
        self._order = {"column": 0, "descending": True}

        def fill():
            order_by(weeks, columns, self._order, tiebreak=lambda w: w[0])
            for index, (week, unrolled, rolled) in enumerate(weeks):
                table.setItem(index, 0, QTableWidgetItem(week))
                table.setItem(index, 1, QTableWidgetItem(
                    _num(unrolled["median"] if unrolled else None)))
                table.setItem(index, 2, QTableWidgetItem(
                    _num(unrolled["pop"] if unrolled else None)))
                table.setItem(index, 3, QTableWidgetItem(
                    _num(rolled["median"] if rolled else None)))
                table.setItem(index, 4, QTableWidgetItem(
                    _num(rolled["avg"] if rolled else None)))
                span = ("—" if rolled is None else
                        f"{_num(rolled['price_min'])} – "
                        f"{_num(rolled['price_max'])}")
                table.setItem(index, 5, QTableWidgetItem(span))
                table.setItem(index, 6, QTableWidgetItem(
                    _num(rolled["stddev"] if rolled else None)))
                table.setItem(index, 7, QTableWidgetItem(
                    _num(rolled["pop"] if rolled else None)))

        fill()
        sortable(table, columns, self._order, fill)
        self.table = table
        layout.addWidget(table)
        layout.addStretch(1)
        # As wide as the eight columns want to be, so the last three are
        # there to be read; the table carries its own bar for a screen that
        # is narrower than that.
        fit_dialog(self, wide=(table,))
