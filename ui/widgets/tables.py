"""The read-only table this application uses everywhere.

Lifted out of the plan page when the fissure page needed the same thing. Every
table in here is the same object with different columns: no editing, no
selection to speak of, one column that takes the slack and a height that fits
its rows exactly, because a table with its own scrollbar inside a page that
already scrolls is two scrollbars arguing.

Two things every table does that a plain QTableWidget does not:

The row under the pointer lights up whole. Qt's `::item:hover` colours the one
cell the mouse is over, which on a seven-column row says almost nothing - the
pointer rests on whichever cell happens to be under it, while the row is what
is meant.
The row is the thing that can be clicked, so the row is the thing that answers.

And every heading can be clicked to reorder. Sorting is done on the data by
the page and the cells are refilled, never by `setSortingEnabled`: that
reorders the widget's own rows behind the page's back, and most tables here
have click handlers that reach into a list by row index - one of them hands
things to the kiosk. The rows and the list would part company on the first
click.

What each table opens on is the page's own answer to the question it was
written to ask: the ducat page alphabetically, because it is read by looking
for a part; the plan page by how close a set is; the riven board by what a
rolled riven fetches. Each of those is a column, so the order a page chose is
always one that can be got back to by clicking.

A cell that cannot show its text says so in two ways. The column that
carries the sentence wraps instead of cutting it, and whatever is still cut
shows its whole text in a tooltip. Before that the long column was the one
Qt squeezed first: at 20pt "What you want from it" had 481 of 1289 pixels and
ended in "Akbolto Pr...".
"""
from __future__ import annotations

import re
from typing import Callable, NamedTuple

from PySide6.QtCore import QEvent, QRect, QTimer, Qt
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QCheckBox, QHeaderView, QStyle, QStyleOptionViewItem, QTableWidget,
    QToolTip,
)

from ui import theme


#: What a figure looks like in a cell: an optional sign, digits with separators,
#: an optional percent sign. Not "12 / 30" and not "NEED 1 of 3" - those are
#: sentences with numbers in them and read from the left.
_FIGURE = re.compile(r"^[+\-\u2212]?\d[\d.,]*\s?%?$")

#: The cells that stand for "no figure" and do not stop a column from being one.
_NO_FIGURE = ("", "\u2014", "-")


def is_figure(text: str) -> bool:
    """Whether a cell's text is a plain number, for aligning it to the right."""
    return bool(_FIGURE.match(text.strip()))


#: The least a wrapping column is allowed to be, in ems of the text, in a table
#: that may scroll sideways. Below it the column keeps this width and the table
#: gets its bar, instead of a sentence squeezed to 3 words a line: at 20pt in a
#: 1600x900 window "What you want from it" was left 81px.
WRAP_MIN_EM = 10

#: Rows are this many lines of text tall at least: a click on one lands on it
#: with room to spare (audit C2; Qt's default of 30px was 1.15 lines at 14pt).
ROW_LINES = 1.25


class Sort(NamedTuple):
    """How one column answers "in what order?".

    `key` reads the value out of whatever the page keeps its rows in - a dict,
    a tuple, a database row - and `descending_first` says which way round the
    column starts when it is first clicked. A name wants to start at A; a pile
    of ducats wants to start at the biggest, because that is the question the
    number is on the page to answer.
    """

    key: Callable
    descending_first: bool = False


class Table(QTableWidget):
    """A table that knows which row the pointer is on."""

    def __init__(self, rows: int, columns: int):
        super().__init__(rows, columns)
        self._hovered = -1
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        # On the viewport, not on the table: the viewport covers the table, so
        # it is the viewport the pointer enters and leaves. A table listening
        # for its own leaveEvent would never hear one, and the last row hovered
        # would stay lit after the mouse had gone.
        self.viewport().installEventFilter(self)
        # Two of these tables are tall enough to scroll. Scrolling moves a
        # different row under a pointer that has not moved.
        self.verticalScrollBar().valueChanged.connect(self._follow_pointer)
        self._colour = None
        # The column whose text wraps, and the height the table keeps. Both
        # are set by simple_table / fit_height and read when the width
        # changes: a window made narrower wraps more, and a table that was
        # fitted to its rows has to be fitted again.
        self.wrap_column: int | None = None
        self.fit_cap: int | None = None
        self.fitted = False
        self.scroll_x = False
        self.stretch_column = 0
        self._widget_columns: set[int] = set()
        self.figure_columns: set[int] = set()
        self._align_later = QTimer(self)
        self._align_later.setSingleShot(True)
        self._align_later.setInterval(0)
        self._align_later.timeout.connect(self.align_figures)
        self._reflow_later = QTimer(self)
        self._reflow_later.setSingleShot(True)
        self._reflow_later.setInterval(0)
        self._reflow_later.timeout.connect(self.reflow)

    def hovered_row(self) -> int:
        return self._hovered

    def setItem(self, row, column, item):
        super().setItem(row, column, item)
        self._align_later.start()
        # A page refills its cells whenever the order changes, so the heights
        # that depend on the text are worked out again after the fill, once
        # for all the cells and not once per cell.
        if self.wrap_column is not None and column == self.wrap_column:
            self._reflow_later.start()

    def setHorizontalHeaderLabels(self, labels):  # noqa: N802 - Qt override
        # A sortable table writes its headings again on every click, which
        # makes new header items and drops the alignment of the old ones.
        super().setHorizontalHeaderLabels(labels)
        self._align_header()

    def align_figures(self) -> None:
        """Right-align every column whose cells are all figures.

        Here, in the one table every page uses, rather than in each page that
        fills one: a column of numbers is read down its right edge, where the
        units line up, and left-aligned "9", "12" and "1,204" do not. A column
        counts as figures when every cell with anything in it is one - a name
        column with a stray "2" does not. A cell a page aligned itself keeps
        what the page chose (audit C6).
        """
        # A cell nobody aligned answers 0 (no value set); one aligned to the
        # default says left and centre. Both are "the page did not choose".
        unset = (0, int(Qt.AlignLeft | Qt.AlignVCenter))
        self.figure_columns = set()
        for column in range(self.columnCount()):
            items = [self.item(row, column) for row in range(self.rowCount())]
            texts = [item.text() for item in items
                     if item is not None and item.text().strip() not in _NO_FIGURE]
            if texts and all(is_figure(text) for text in texts):
                self.figure_columns.add(column)
                for item in items:
                    if item is not None and int(item.textAlignment()) in unset:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._align_header()

    def _align_header(self) -> None:
        for column in range(self.columnCount()):
            heading = self.horizontalHeaderItem(column)
            if heading is not None and column in self.figure_columns:
                heading.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def setCellWidget(self, row, column, widget):
        super().setCellWidget(row, column, widget)
        self._widget_columns.add(column)
        self._reflow_later.start()

    def sizeHintForColumn(self, column):
        """What a column of cell widgets asks for, as well as its text.

        Qt measures a column from its items and ignores the widgets set over
        them. The check box in the mastery list's first column has no text
        and its heading is empty, so the column came out at 38px and the box,
        which wants 46, was squeezed in 12 rows.
        """
        hint = super().sizeHintForColumn(column)
        if column in self._widget_columns:
            for row in range(self.rowCount()):
                widget = self.cellWidget(row, column)
                if widget is not None:
                    hint = max(hint, cell_padding(self, column)[0]
                               + max(widget.sizeHint().width(),
                                     widget.minimumSizeHint().width()))
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.wrap_column is not None or (self.fitted and self.scroll_x):
            self._reflow_later.start()


    def showEvent(self, event):
        super().showEvent(event)
        if self.wrap_column is not None:
            self._reflow_later.start()

    def reflow(self) -> None:
        """Give the wrapping column the rows its text needs, then re-fit."""
        # The columns of widgets, now that the widgets are in them. Sections
        # are measured when rows are added and the widgets come after.
        head = self.horizontalHeader()
        if self.scroll_x and self.wrap_column is not None:
            # The sentence column takes what the others leave, and no less than
            # WRAP_MIN_EM; when they leave less, the table scrolls sideways.
            # What the other columns really take: the size they have, not only
            # what their text asks for (a column with a delegate or a widget
            # takes more, and the sentence column was then left 124px).
            others = 2 * self.frameWidth() + sum(
                max(head.sectionSize(c), head.sectionSizeHint(c),
                    self.sizeHintForColumn(c))
                for c in range(self.columnCount())
                if c != self.wrap_column and not self.isColumnHidden(c))
            if self.verticalScrollBar().isVisible():
                others += self.verticalScrollBar().width()
            # Never under its own heading either: "WHAT YOU WANT FROM IT"
            # wants 341px at 20pt and the 10em floor is 267.
            floor = max(theme.px(WRAP_MIN_EM), head.sectionSizeHint(self.wrap_column))
            if self.width() - others >= floor:
                head.setSectionResizeMode(self.wrap_column, QHeaderView.Stretch)
            else:
                head.setSectionResizeMode(self.wrap_column,
                                          QHeaderView.Interactive)
                self.setColumnWidth(self.wrap_column, floor)
        elif self.scroll_x:
            # A table that may scroll sideways keeps every column at the width
            # of its text once they no longer fit: the column that takes the
            # slack has no slack to take, and as a stretch section it would be
            # squeezed under its own text instead of the bar appearing.
            roomy = natural_width(self) <= self.width()
            head.setSectionResizeMode(
                self.stretch_column,
                QHeaderView.Stretch if roomy else QHeaderView.ResizeToContents)
        for widget_column in self._widget_columns:
            if head.sectionResizeMode(widget_column) == QHeaderView.ResizeToContents:
                self.resizeColumnToContents(widget_column)
        column = self.wrap_column
        # Only once there is a width to wrap to. A table that has not been
        # shown has the width of a default widget, and rows worked out against
        # that are several times too tall.
        if column is not None and self.isVisible():
            base = self.verticalHeader().defaultSectionSize()
            fm = self.fontMetrics()
            pad_w, pad_h = cell_padding(self, column)
            # One pixel of slack, not two: a column sized to its text is as
            # wide as the text plus padding and `boundingRect` wraps a line
            # that fits to the pixel, but the padding is not the same for
            # every cell (36px for one, 37 for the next, measured), and a text
            # that fits the first one's room exactly is cut in the second's.
            room = self.columnWidth(column) - pad_w + 1
            for row in range(self.rowCount()):
                item = self.item(row, column)
                need = base
                if item is not None and item.text() and room > 0:
                    box = fm.boundingRect(QRect(0, 0, room, 100000),
                                          Qt.TextWordWrap, item.text())
                    # A line of text sits in a row of the default height. Two
                    # or more need the room the theme's cell padding takes
                    # as well, or the last line is drawn as "...".
                    if box.height() > fm.lineSpacing() * 1.5:
                        need = max(base, box.height() + pad_h)
                if self.rowHeight(row) != need:
                    self.setRowHeight(row, need)
        if self.fitted:
            fit_height(self, self.fit_cap)

    def viewportEvent(self, event):
        """The whole text of a cell that cannot show it.

        Wrapping makes this rare; it is what is left for a single word wider
        than its column and for the cells of a table that is not allowed to
        grow. A cell with a tooltip of its own keeps it.
        """
        if event.type() == QEvent.ToolTip:
            index = self.indexAt(event.pos())
            item = self.item(index.row(), index.column()) if index.isValid() else None
            if item is not None and not item.toolTip() and cell_is_cut(self, item):
                QToolTip.showText(event.globalPos(), item.text(), self.viewport())
                return True
        return super().viewportEvent(event)

    def paintEvent(self, event):
        """The hovered row, painted whole and painted first.

        Under the cells rather than through a delegate. A delegate only gets
        the columns that have no delegate of their own, and two of these
        tables draw their progress column themselves - the lit row came out
        with a hole in it exactly where the bar was. It also reaches past the
        last column, so a table narrower than the width it was given lights
        all the way across instead of stopping mid-row.

        Nothing is drawn over: every cell background in this theme is
        transparent, so the fill shows through the row it is under.
        """
        if self._hovered >= 0:
            if self._colour is None:
                # Every one of these tables lives inside a card today, but a
                # card is lighter than the page and hover has to mean "up"
                # either way. Asked on the first paint: a table has no parent
                # while it is being built, so there is nothing to ask before.
                self._colour = QColor(theme.CARD_HOVER if self._inside_a_card()
                                      else theme.BG_ELEVATED)
            painter = QPainter(self.viewport())
            painter.fillRect(0, self.rowViewportPosition(self._hovered),
                             self.viewport().width(),
                             self.rowHeight(self._hovered), self._colour)
            painter.end()
        super().paintEvent(event)

    def _inside_a_card(self) -> bool:
        parent = self.parentWidget()
        while parent is not None:
            if parent.objectName() == "Card":
                return True
            parent = parent.parentWidget()
        return False

    def eventFilter(self, watched, event):
        if watched is self.viewport():
            if event.type() == QEvent.MouseMove:
                self._hover(self.rowAt(int(event.position().y())))
            elif event.type() == QEvent.Leave:
                self._hover(-1)
        return super().eventFilter(watched, event)

    def _follow_pointer(self) -> None:
        local = self.viewport().mapFromGlobal(QCursor.pos())
        self._hover(self.rowAt(local.y())
                    if self.viewport().rect().contains(local) else -1)

    def _hover(self, row: int) -> None:
        if row == self._hovered:
            return
        self._hovered = row
        self.viewport().update()


def cell_padding(table: QTableWidget, column: int) -> tuple[int, int]:
    """Pixels a cell in `column` spends on padding, across and down.

    From the theme's own rule, asked of the delegate rather than copied from
    the stylesheet, so a change to the cell padding there is followed here.
    The rule is one for every cell, so when `column` has no text to measure (a
    column of check boxes) any other column's text answers for it.
    """
    columns = [column] + [c for c in range(table.columnCount()) if c != column]
    fm = table.fontMetrics()
    for probe in columns:
        for row in range(table.rowCount()):
            item = table.item(row, probe)
            if item is None or not item.text():
                continue
            option = QStyleOptionViewItem()
            option.initFrom(table)
            option.font = table.font()
            option.widget = table
            hint = table.itemDelegate().sizeHint(
                option, table.model().index(row, probe))
            return (max(0, hint.width() - fm.horizontalAdvance(item.text())),
                    max(0, hint.height() - fm.height()))
    return (0, 0)


def cell_is_cut(table: QTableWidget, item) -> bool:
    """Whether the cell of `item` shows less than all of its text."""
    index = table.indexFromItem(item)
    row, column = index.row(), index.column()
    width = sum(table.columnWidth(c)
                for c in range(column, column + table.columnSpan(row, column)))
    height = sum(table.rowHeight(r)
                 for r in range(row, row + table.rowSpan(row, column)))
    fm = table.fontMetrics()
    pad_w, pad_h = cell_padding(table, column)
    room = width - pad_w + 2
    if table.wordWrap():
        need = fm.boundingRect(QRect(0, 0, max(room, 1), 100000),
                               Qt.TextWordWrap, item.text())
        # One line is drawn in the row whatever its padding; two and more
        # have to fit inside it.
        lines = need.height() / max(fm.lineSpacing(), 1)
        too_tall = lines > 1.5 and need.height() + pad_h > height + 1
        return too_tall or need.width() > room + 1
    return fm.horizontalAdvance(item.text()) > room


def natural_width(table: QTableWidget) -> int:
    """The width at which no column of `table` has to give up any text.

    The sum of what each column asks for, heading included, plus the frame and
    a vertical scrollbar if the table may show one. A dialog sizes itself from
    this so that its table needs neither a horizontal scrollbar nor a cut.
    """
    head = table.horizontalHeader()
    width = 2 * table.frameWidth()
    for column in range(table.columnCount()):
        if table.isColumnHidden(column):
            continue
        width += max(head.sectionSizeHint(column),
                     table.sizeHintForColumn(column))
    if table.verticalScrollBarPolicy() != Qt.ScrollBarAlwaysOff:
        width += table.style().pixelMetric(QStyle.PM_ScrollBarExtent)
    return width


def rows_height(table: QTableWidget, rows: int) -> int:
    """Height of the heading and `rows` rows, in the row height the table has."""
    row = (table.rowHeight(0) if table.rowCount()
           else table.verticalHeader().defaultSectionSize())
    return (table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth()
            + rows * row)


def keep_rows(table: QTableWidget, rows: int) -> None:
    """Never let a table that may scroll get shorter than `rows` rows.

    In rows, not pixels: a pixel minimum was 320 at every text size, which is
    ten rows at 11pt and seven at 20pt. Called after the row height is final -
    `check_rows` changes it.
    """
    table.setMinimumHeight(rows_height(table, rows))


def simple_table(headers: list[str], rows: int, stretch: int = 0,
                 fit: bool = True, wrap: bool = False,
                 min_rows: int | None = None,
                 scroll_x: bool = False) -> Table:
    """The table, with `fit` deciding whether it is as tall as its rows.

    Almost always it should be: these tables sit in pages that scroll, and a
    scrollbar inside a scrollbar is two scrollbars arguing. The exception is a
    table that is handed the whole page - the inventory's four hundred sets -
    where the outer page does not scroll and the table is the thing that does.

    Every column takes the width of its contents except `stretch`, which takes
    what is left. `wrap` says that column carries sentences: when the room
    runs out it breaks the line and the row grows, where otherwise the text
    would end in "...". `min_rows` is for a table that is not fitted: it keeps
    that many rows however little height the dialog leaves it. `scroll_x` is
    for a dialog that may be smaller than its table is wide (a screen narrower
    than eight columns): the bar appears only then.
    """
    table = Table(rows, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setAlternatingRowColors(True)
    # The grid is already invisible - the theme paints its lines in nothing -
    # but it still holds a pixel between every pair of cells, and a hovered
    # row lit cell by cell came out striped along those pixels.
    table.setShowGrid(False)
    # Rows tall enough to hit: Qt's 30px default was 1.15 lines of text at
    # 14pt and 1.1 at 20pt. Never lower than what Qt chose, for a font where
    # that is already more.
    rows_v = table.verticalHeader()
    rows_v.setDefaultSectionSize(max(
        rows_v.defaultSectionSize(),
        round(table.fontMetrics().lineSpacing() * ROW_LINES)))
    head = table.horizontalHeader()
    head.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    head.setSectionResizeMode(QHeaderView.ResizeToContents)
    head.setSectionResizeMode(stretch, QHeaderView.Stretch)
    table.scroll_x = scroll_x
    table.stretch_column = stretch
    if wrap:
        table.wrap_column = stretch
    if fit:
        fit_height(table)
    elif min_rows:
        keep_rows(table, min_rows)
    return table


def check_rows(table: QTableWidget,
               box_for_row: Callable[[int], QCheckBox | None],
               typed_columns: tuple[int, ...] = ()) -> None:
    """Make a table whose rows carry a check box easy to hit. Call once.

    Every table with a box per row had the same two faults. The
    rows kept Qt's default 30px while the theme's cell padding grows with the
    text, which squeezed the box to a flat line from about 14pt up; and the
    box was the only target in a row a whole window wide. Rows now take the
    height of a row with a picture, and a click anywhere on a row flips its
    box - except in `typed_columns`, cells that are typed into rather than
    chosen. A click on the box itself is the box's own and is not counted
    twice: it never reaches the table.
    """
    fit_check_rows(table)

    def flip(row: int, column: int) -> None:
        if column in typed_columns:
            return
        box = box_for_row(row)
        if box is not None and box.isEnabled():
            box.setChecked(not box.isChecked())

    table.cellClicked.connect(flip)


def fit_check_rows(table: QTableWidget) -> None:
    """Row height for check-box rows at the current text size.

    Separate from check_rows() because a table rebuilt after a text-size
    change needs the new height, and connecting the click again would flip
    every row twice.
    """
    from ui.widgets import thumbs

    table.verticalHeader().setDefaultSectionSize(thumbs.row_height(table))


def fit_height(table: QTableWidget, max_rows: int | None = None) -> QTableWidget:
    """Make a table exactly as tall as the rows it holds.

    Called again by any page that changes its row height after building - a
    column of pictures is taller than a column of words, and a table sized
    before the pictures were asked for is a table missing its last rows.

    `max_rows` is for a table that is longer than a page should give it (the
    fissure openings, the missing pieces, the riven board): past that many rows
    it stops growing and scrolls. In rows and not in pixels, because a pixel
    cap was 5 rows at 20pt where it had been 8 at 11pt, and the rows are what
    is read. A table made by `simple_table` fits itself again when its width
    changes, because a wrapped row is taller in a narrower window.
    """
    table.fit_cap = max_rows
    table.fitted = True
    # The vertical policy is set once, after the rows are counted. Switching
    # it off and back on for a capped table hid and showed the bar on every
    # call, which resized the viewport twice, and a resize schedules the
    # reflow that calls this again: the Plan page's 153 pieces kept one core
    # at 100 % for as long as the page was open.
    # No bar along the bottom either: every column here is sized to its
    # contents bar one that takes the slack, so a horizontal scrollbar can
    # only be a few pixels of rounding - and it would eat a row's worth of
    # the height just worked out. The exception is a table that asked for it
    # (`scroll_x`, a dialog narrower than its columns), and then only while
    # the columns really are wider than the table.
    bar = 0
    if getattr(table, "scroll_x", False):
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        if table.horizontalHeader().length() > table.width() - 2 * table.frameWidth():
            bar = table.horizontalScrollBar().sizeHint().height()
    else:
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    height = table.horizontalHeader().height() + 2 * table.frameWidth() + bar
    capped = max_rows is not None and table.rowCount() > max_rows
    for row in range(max_rows if capped else table.rowCount()):
        height += table.rowHeight(row)
    table.setVerticalScrollBarPolicy(
        Qt.ScrollBarAsNeeded if capped else Qt.ScrollBarAlwaysOff)
    table.setFixedHeight(height)
    return table


def order_by(rows: list, columns: dict[int, Sort], order: dict,
             tiebreak=None) -> None:
    """Put `rows` in the order the header currently shows, in place.

    In place because the click handlers on these tables hold the same list and
    read it by row index. Handing back a new list would leave them pointing at
    the order the table had before the header was touched.

    Sorted twice, and the first pass is the tie-break: Python's sort is stable,
    so equal rows settle underneath whatever the chosen column says. Fifteen
    parts worth fifteen ducats each are then in an order a person can read, in
    both directions, without the tie-break flipping with them.

    `tiebreak` defaults to the first column, which on most of these tables is
    the name. Where a page's own order is two or three things deep - fissures
    by era and then by how soon they end - it is that remainder, so the column
    the page opens on reproduces the order the page chose.
    """
    column = columns.get(order["column"])
    if column is None:
        return
    if tiebreak is None:
        first = columns.get(0)
        tiebreak = first.key if first is not None else None
    if tiebreak is not None:
        rows.sort(key=tiebreak)
    rows.sort(key=column.key, reverse=order["descending"])


def sortable(table: Table, columns: dict[int, Sort], order: dict,
             redraw: Callable[[], None]) -> None:
    """Let the header choose the order, and show which one it chose.

    `order` is the page's, not the table's, and is mutated here: these pages
    are rebuilt whole every time something leaves the shelf or an import
    lands, and an order that lived on the widget would go back to the default
    every time the user did anything.
    """
    head = table.horizontalHeader()
    head.setSectionsClickable(True)
    head.setCursor(Qt.PointingHandCursor)
    # Qt's own indicator is drawn at the far end of the section, which on the
    # column that takes the slack is half a table away from the word it is
    # about. Written into the heading instead, where it is read as part of it.
    head.setSortIndicatorShown(False)
    labels = [table.horizontalHeaderItem(index).text()
              if table.horizontalHeaderItem(index) else ""
              for index in range(table.columnCount())]
    # Qt's own answer to a clicked header, while sorting is disabled, is to
    # select the column. On a page where a click on a row opens that item,
    # a whole column lighting up is an answer to a question nobody asked.
    try:
        head.sectionPressed.disconnect()
    except (RuntimeError, TypeError):
        pass

    def show():
        arrow = " \u25be" if order["descending"] else " \u25b4"
        table.setHorizontalHeaderLabels(
            [label + (arrow if index == order["column"] else "")
             for index, label in enumerate(labels)])

    def clicked(column: int):
        if column not in columns:
            return
        if column == order["column"]:
            order["descending"] = not order["descending"]
        else:
            order["column"] = column
            order["descending"] = columns[column].descending_first
        show()
        redraw()

    show()
    head.sectionClicked.connect(clicked)
    # Left on the widget so a check can find every table that claims to be
    # reorderable and press all of its headings. Forty-odd of these keys are
    # one-line lambdas reaching into a row by name or by position, and a
    # wrong one does not fail until somebody clicks that column.
    table.sortable_columns = columns
