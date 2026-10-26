"""A set's progress, drawn rather than spelled.

"3/4" is a thing you read; a bar three quarters full is a thing you see. On a
page whose whole job is "how close am I", every row asking to be read is a page
that has to be worked through rather than scanned.

Drawn with QPainter because a stylesheet cannot put two shapes in one cell, and
as a delegate rather than a widget per row because a table of six hundred rows
with six hundred child widgets is a table that takes a second to open.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyledItemDelegate

from ui import theme

#: The value a cell carries for this delegate: (held, total).
PROGRESS_ROLE = Qt.UserRole + 17

#: How tall the bar is against the row, and how much of the cell the number
#: keeps to itself. The number stays - a bar says "nearly" where somebody
#: filling a set needs to know whether nearly means one piece or two.
BAR_HEIGHT = 0.34
NUMBER_SHARE = 0.42


class ProgressDelegate(QStyledItemDelegate):
    """Draws `(held, total)` as a bar with the figures beside it."""

    def paint(self, painter: QPainter, option, index):
        value = index.data(PROGRESS_ROLE)
        if not value:
            super().paint(painter, option, index)
            return
        held, total = value
        if not total:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(option.rect)
        text_width = rect.width() * NUMBER_SHARE
        number = QRectF(rect.left(), rect.top(), text_width, rect.height())
        track = QRectF(rect.left() + text_width + theme.px(0.3), 0,
                       max(theme.px(1.0), rect.width() - text_width - theme.px(0.6)),
                       max(3.0, rect.height() * BAR_HEIGHT))
        track.moveTop(rect.top() + (rect.height() - track.height()) / 2)

        share = min(1.0, held / total)
        colour = QColor(theme.GOOD if held >= total
                        else theme.WARN if held else theme.TEXT_FAINT)

        radius = track.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.BG_ELEVATED))
        painter.drawRoundedRect(track, radius, radius)
        if share > 0:
            filled = QRectF(track)
            filled.setWidth(max(track.height(), track.width() * share))
            painter.setBrush(colour)
            painter.drawRoundedRect(filled, radius, radius)

        painter.setPen(colour)
        painter.drawText(number, Qt.AlignVCenter | Qt.AlignLeft,
                         f"{held}/{total}")
        painter.restore()


def set_progress(item, held: int, total: int) -> None:
    """Put the pair on a cell, and the text under it for anything that sorts."""
    item.setData(PROGRESS_ROLE, (held, total))
    item.setText(f"{held}/{total}" if total else "-")

def fit_column(table, column: int) -> None:
    """Give a progress column room for a bar rather than a dot.

    `ResizeToContents` measures the text - "1/2" - and the bar drawn beside it
    is then squeezed into whatever is left, which at that width is a dot. The
    delegate cannot ask for more on its own; the column has to be told.

    The heading has to fit too, and it is the wider of the two here: a fixed
    width worked out for the bar alone cut the sort arrow off the end of
    "PARTS HELD", so the column that was sorting the page was the one column
    that could not say so.
    """
    from PySide6.QtWidgets import QHeaderView

    header = table.horizontalHeader()
    header.setSectionResizeMode(column, QHeaderView.Fixed)
    bar = table.fontMetrics().horizontalAdvance("00/00") * 2 + theme.px(1.0)
    item = table.horizontalHeaderItem(column)
    # Upper case because the stylesheet draws it that way, and with room for
    # an arrow whether this column carries one at the moment or not - the
    # arrow moves from column to column as the headings are clicked, and a
    # width that changed with it would shuffle the table sideways.
    heading = (item.text().upper() + " \u25be") if item is not None else ""
    label = (header.fontMetrics().horizontalAdvance(heading)
             + theme.px(1.5) + len(heading) * 2)
    header.resizeSection(column, max(bar, label))
