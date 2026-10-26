"""Layout detectors: does a shown widget show everything it holds?

Lifted from an interface audit. Each
function takes a root widget that is on screen and returns a list of findings;
none of them changes anything. They measure what the widget shows against
what it needs: a label against its text, a widget against its own
`minimumSizeHint`, a table cell against its column, a table against the rows
it was meant to show.

Used by tests/test_ui_smoke.py, which calls `findings(root)`. The detectors
cover cut text, widgets squeezed under their own minimum, cut table cells and
short tables, and the click targets (`click_targets`), which
run on every page and dialog the same way. The radio indicator and the combo
arrow are measured on a widget of their own by the checks that need them
(`indicator_size`, `combo_arrow_width`).

Everything is measured, nothing is guessed: a finding carries the pixels the
widget has and the pixels it needs.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractScrollArea, QAbstractSpinBox, QCheckBox,
    QComboBox, QGroupBox, QLabel, QLineEdit, QListWidget, QRadioButton, QStyle,
    QStyleOptionButton, QStyleOptionComboBox, QStyleOptionViewItem, QTabBar,
    QTableWidget, QWidget,
)

#: Pixels of slack before something counts as cut. Qt rounds text widths up
#: and layouts give the odd pixel to either side.
TOL = 2

#: How many lines of text tall a thing to click must be. The audit's
#: measure of "hit without aiming": 1.2 lines.
CLICK_LINES = 1.2

#: Rows a table must show, or all of them if it has fewer. A table that shows
#: less than this is a heading with a keyhole under it.
MIN_ROWS = 6


def _snip(text, n=48):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "~"


def where(widget, stop=None) -> str:
    """The widget's class chain up to `stop`, with object names and its text."""
    chain = []
    current = widget
    while current is not None and current is not stop and len(chain) < 7:
        name = type(current).__name__
        if current.objectName():
            name += "#" + current.objectName()
        chain.append(name)
        current = current.parentWidget()
    chain.reverse()
    text = ""
    for attribute in ("text", "placeholderText", "toolTip"):
        getter = getattr(widget, attribute, None)
        if callable(getter):
            try:
                text = getter()
            except Exception:  # noqa: BLE001 - a label for a message only
                text = ""
            if text:
                break
    return " > ".join(chain) + (f"  '{_snip(text)}'" if text else "")


def _shown(widget) -> bool:
    return widget.isVisible() and widget.width() > 0 and widget.height() > 0


def _widgets(root, kinds):
    kinds = kinds if isinstance(kinds, tuple) else (kinds,)
    return [w for w in root.findChildren(QWidget)
            if isinstance(w, kinds) and _shown(w)]


def _found(kind, widget, root, have, need, note=""):
    return {"kind": kind, "where": where(widget, root), "have": have,
            "need": need, "note": note}


def label_clips(root) -> list:
    """Labels whose text is taller (or, unwrapped, wider) than the label."""
    out = []
    for label in _widgets(root, QLabel):
        if not label.text().strip():
            continue
        if label.pixmap() is not None and not label.pixmap().isNull():
            continue
        if label.wordWrap():
            need = label.heightForWidth(label.width())
            if need > label.height() + TOL:
                out.append(_found("label_wrapped_height", label, root,
                                  f"{label.width()}x{label.height()}",
                                  f"h {need}"))
        else:
            hint = label.sizeHint()
            if hint.height() > label.height() + TOL:
                out.append(_found("label_height", label, root,
                                  label.height(), hint.height()))
            # A label that shortens itself (ElidedLabel, the Settings footer)
            # is narrower than its text on purpose and carries all of it as
            # its tooltip.
            elided = hasattr(label, "fullText")
            if elided and label.toolTip() != label.fullText():
                out.append(_found("label_elided_without_tooltip", label, root,
                                  label.toolTip(), label.fullText()))
            if hint.width() > label.width() + TOL and not elided:
                out.append(_found("label_width", label, root,
                                  label.width(), hint.width()))
    return out


def below_min_hint(root) -> list:
    """Widgets smaller than their own `minimumSizeHint`.

    The layout never does this on purpose: it happens when the parent is too
    small for the sum of its children and Qt squeezes them below what they said
    they need. Wrapped labels are left to `label_clips`, which measures them
    properly - their minimum hint is a line, not the paragraph.
    """
    out = []
    kinds = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QLabel,
             QGroupBox)
    for widget in _widgets(root, kinds):
        if isinstance(widget, QLabel) and widget.wordWrap():
            continue
        minimum = widget.minimumSizeHint()
        if widget.height() + TOL < minimum.height():
            out.append(_found("below_min_height", widget, root,
                              widget.height(), minimum.height()))
        if widget.width() + TOL < minimum.width():
            out.append(_found("below_min_width", widget, root,
                              widget.width(), minimum.width()))
    return out


def _padding(table, row, column):
    """The theme's cell padding (across, down), asked of the delegate."""
    option = QStyleOptionViewItem()
    option.initFrom(table)
    option.font = table.font()
    option.widget = table
    index = table.model().index(row, column)
    hint = table.itemDelegate().sizeHint(option, index)
    text = index.data() or ""
    fm = table.fontMetrics()
    return (max(0, hint.width() - fm.horizontalAdvance(text)),
            max(0, hint.height() - fm.height()))


def table_cells(root, max_rows=400) -> list:
    """Cells that cannot show their text, and columns nobody can reach.

    A cell is cut when its text, wrapped to its column, is wider or taller
    than the cell - or, in a table that does not wrap, wider than the column.
    Measured by `QFontMetrics`, not by the delegate's size hint: the hint is
    the width of the text on one line, which says "cut" about every cell that
    wraps and is the reason this check measures wrapped text itself.
    """
    out = []
    for table in _widgets(root, QTableWidget):
        head = table.horizontalHeader()
        view = table.viewport().width()
        total = sum(table.columnWidth(c) for c in range(table.columnCount())
                    if not table.isColumnHidden(c))
        bar_off = table.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        if total > view + TOL and bar_off:
            out.append(_found("table_columns_unreachable", table, root, view,
                              total, "columns wider than the table, no "
                              "horizontal scrollbar to reach them"))
        vbar = table.verticalScrollBar()
        if vbar.maximum() > 0 and (table.verticalScrollBarPolicy()
                                   == Qt.ScrollBarAlwaysOff):
            out.append(_found("table_rows_unreachable", table, root,
                              table.viewport().height(),
                              table.viewport().height() + vbar.maximum(),
                              "rows below the table, no scrollbar"))
        fm = table.fontMetrics()
        cut_columns: dict[int, list] = {}
        for row in range(min(table.rowCount(), max_rows)):
            if table.isRowHidden(row):
                continue
            for column in range(table.columnCount()):
                if table.isColumnHidden(column):
                    continue
                item = table.item(row, column)
                if item is None or not item.text():
                    continue
                # The cell's own width, across the columns it spans: not
                # `visualRect`, which is empty for a row scrolled out of view.
                width = sum(table.columnWidth(c) for c in range(
                    column, column + table.columnSpan(row, column)))
                height = sum(table.rowHeight(r) for r in range(
                    row, row + table.rowSpan(row, column)))
                pad_w, pad_h = _padding(table, row, column)
                room = width - pad_w + TOL
                if table.wordWrap():
                    need = fm.boundingRect(QRect(0, 0, max(room, 1), 100000),
                                           Qt.TextWordWrap, item.text())
                    lines = need.height() / max(fm.lineSpacing(), 1)
                    cut = (need.width() > room + TOL or
                           (lines > 1.5 and need.height() + pad_h
                            > height + TOL))
                    wanted = need.width() + pad_w
                else:
                    wanted = fm.horizontalAdvance(item.text()) + pad_w
                    cut = wanted > width + TOL
                if cut:
                    entry = cut_columns.setdefault(column, [0, 0, ""])
                    entry[0] += 1
                    if wanted > entry[1]:
                        entry[1], entry[2] = wanted, item.text()
        for column, (count, wanted, sample) in cut_columns.items():
            header = table.horizontalHeaderItem(column)
            out.append(_found(
                "cell_text_cut", table, root, table.columnWidth(column), wanted,
                f"column {column} '{header.text() if header else ''}': "
                f"{count} cell(s), worst '{_snip(sample, 40)}'"))
        for index in range(head.count()):
            if head.isSectionHidden(index):
                continue
            need = head.sectionSizeHint(index)
            if need > head.sectionSize(index) + TOL:
                out.append(_found("header_cut", table, root,
                                  head.sectionSize(index), need,
                                  f"section {index}"))
    return out


def table_height(root, minimum=MIN_ROWS, floors=None) -> list:
    """Tables that show fewer rows than they should.

    A table shows `minimum` rows, or all of them if it has fewer. `floors`
    maps the table (by identity) to a higher number for the ones that were
    promised more: the apply table keeps ten, the mastery list six.
    """
    out = []
    floors = floors or {}
    for table in _widgets(root, QTableWidget):
        if not table.rowCount():
            continue
        # Rows that are whole inside the viewport, counted by their own
        # heights: a table with wrapped rows has rows of different heights.
        shown, used = 0, 0
        for row in range(table.rowCount()):
            used += table.rowHeight(row)
            if used > table.viewport().height():
                break
            shown += 1
        want = min(table.rowCount(), floors.get(table, minimum))
        if shown < want:
            out.append(_found("table_rows_shown", table, root, shown, want,
                              f"{table.rowCount()} rows, first {table.rowHeight(0)}px"
                              f", in {table.viewport().height()}px"))
    return out


def table_look(root) -> list:
    """Tables that are not the shared one, or do not look like it.

    Six tables were built by hand and differed from `tables.simple_table` in
    four ways a person sees: grid lines that leave a pixel between cells, no
    light on the row under the pointer, a click that selects one cell where the
    others select the row, and a heading centred where the others are left
    aligned. A table that is the shared class cannot differ in the
    first two; the other two are checked on it.
    """
    from ui.widgets.tables import Table

    out = []
    for table in _widgets(root, QTableWidget):
        problems = []
        if not isinstance(table, Table):
            problems.append("not a tables.Table (no row hover)")
        if table.showGrid():
            problems.append("grid on")
        if table.selectionBehavior() != QTableWidget.SelectRows:
            problems.append("selects cells, not rows")
        if not table.horizontalHeader().defaultAlignment() & Qt.AlignLeft:
            problems.append("heading not left aligned")
        if problems:
            out.append(_found("table_look", table, root, ", ".join(problems),
                              "the shared table"))
    return out


def tab_bars(root) -> list:
    """Tabs that do not fit their bar."""
    out = []
    for bar in _widgets(root, QTabBar):
        for index in range(bar.count()):
            need = bar.tabSizeHint(index).width()
            have = bar.tabRect(index).width()
            if need > have + TOL:
                out.append(_found("tab_cut", bar, root, have, need,
                                  bar.tabText(index)))
    return out


def scroll_overflow(root) -> list:
    """Scroll areas whose content is wider than they are (not tables)."""
    out = []
    for area in _widgets(root, QAbstractScrollArea):
        if isinstance(area, QTableWidget):
            continue
        bar = area.horizontalScrollBar()
        if bar.isVisible() or bar.maximum() > 0:
            out.append(_found("hscroll", area, root, area.viewport().width(),
                              area.viewport().width() + bar.maximum(),
                              "content wider than the area"))
    return out


def click_targets(root, lines: float = CLICK_LINES) -> list:
    """Buttons, boxes, fields and rows that are lower than `lines` lines of text.

    A control lower than a line and a bit is hit by aiming, and on a 4K screen
    at 20pt that is the difference between a click and a miss.
    Check boxes and radio buttons count by their widget, not by the indicator
    inside; the rows of a table or a list by the smallest row. The inner edit of
    a spin box or combo is skipped: the outer widget is the target.
    """
    out = []
    kinds = (QAbstractButton, QComboBox, QAbstractSpinBox, QLineEdit)
    for widget in _widgets(root, kinds):
        if widget.objectName().startswith("qt_") and isinstance(
                widget.parentWidget(), (QAbstractSpinBox, QComboBox)):
            continue
        need = round(widget.fontMetrics().lineSpacing() * lines)
        if widget.height() < need:
            out.append(_found("target_height", widget, root, widget.height(),
                              need))
        if (isinstance(widget, QAbstractButton)
                and not isinstance(widget, (QCheckBox, QRadioButton))
                and widget.width() < need):
            out.append(_found("target_width", widget, root, widget.width(),
                              need))
    for table in _widgets(root, QTableWidget):
        if table.rowCount():
            need = round(table.fontMetrics().lineSpacing() * lines)
            shortest = min(table.rowHeight(row)
                           for row in range(min(table.rowCount(), 50)))
            if shortest < need:
                out.append(_found("row_height", table, root, shortest, need,
                                  f"{table.rowCount()} rows"))
    for listing in _widgets(root, QListWidget):
        need = round(listing.fontMetrics().lineSpacing() * lines)
        heights = [listing.visualItemRect(listing.item(row)).height()
                   for row in range(min(listing.count(), 50))
                   if not listing.item(row).isHidden()]
        # Items that carry a widget of their own (the review rows) are as tall
        # as that widget; it is the plain text rows this is about.
        if heights and min(heights) < need and not any(
                listing.itemWidget(listing.item(row)) is not None
                for row in range(min(listing.count(), 50))):
            out.append(_found("row_height", listing, root, min(heights), need,
                              f"{listing.count()} items"))
    return out


def indicator_size(app, points: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """The indicator of a radio button when not chosen and when chosen, in px.

    Measured as the style reports the indicator's rectangle, on a button of its
    own at `points`. A chosen one used to be 37px against 27 at 20pt: its ring
    was a thicker border outside a width that had not shrunk to make room.
    """
    from ui import theme

    app.setStyleSheet(theme.apply(theme.active(), points))
    sizes = []
    for checked in (False, True):
        radio = QRadioButton("x")
        radio.setChecked(checked)
        radio.show()
        app.processEvents()
        option = QStyleOptionButton()
        radio.initStyleOption(option)
        rect = radio.style().subElementRect(
            QStyle.SE_RadioButtonIndicator, option, radio)
        sizes.append((rect.width(), rect.height()))
        radio.close()
    return sizes[0], sizes[1]


def combo_arrow_width(app, points: int) -> tuple[int, int]:
    """The width of the drop-down part of a combo box and the line it sits by.

    The drop-down was 16px at every text size beside a 26px line at 14pt and a
    37px one at 20pt: a dot of an arrow with a dark stripe next to it.
    """
    from ui import theme

    app.setStyleSheet(theme.apply(theme.active(), points))
    combo = QComboBox()
    combo.addItems(["a", "b"])
    combo.resize(300, combo.sizeHint().height())
    combo.show()
    app.processEvents()
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    arrow = combo.style().subControlRect(
        QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, combo)
    line = combo.fontMetrics().lineSpacing()
    combo.close()
    return arrow.width(), line


#: What `findings` runs on a shown widget. The click targets are not in here:
#: they are a measure of controls, not of cut text, and the check that runs
#: them lists them under their own name.
DETECTORS = (label_clips, below_min_hint, table_cells, table_height, table_look,
             tab_bars, scroll_overflow)


def findings(root, floors=None) -> list:
    """Every finding of every detector on `root`, each tagged by detector."""
    out = []
    for detector in DETECTORS:
        found = (detector(root, floors=floors) if detector is table_height
                 else detector(root))
        out += [dict(f, detector=detector.__name__) for f in found]
    return out
