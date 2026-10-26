"""The diamond mark.

Warframe flags a void reward with a diamond, it is already the shape of this
application's icon, and here it marks the head of a section and the selected
page. One symbol, one meaning throughout: this one.

Drawn rather than set as a character. The obvious alternative is to put "◆" in
the label text, which depends on the font having the glyph, inherits the text
colour and baseline, and cannot be sized independently of the words beside it.
A painted diamond has none of those problems and is a dozen lines.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import QWidget

from ui import theme


def pixmap(colour: str, size: int = 10, box: int = 16):
    """A diamond as a QPixmap, for places that take an icon rather than a widget.

    The navigation list is one: QListWidget items hold an icon, not a child
    widget, and marking the current page is exactly what the shape is for.
    """
    from PySide6.QtGui import QPixmap

    image = QPixmap(box, box)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    centre = box / 2
    half = size / 2
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(colour))
    painter.drawPolygon(QPolygonF([
        QPointF(centre, centre - half), QPointF(centre + half, centre),
        QPointF(centre, centre + half), QPointF(centre - half, centre),
    ]))
    painter.end()
    return image


class Diamond(QWidget):
    """A small filled diamond, vertically centred in its box."""

    def __init__(self, colour: str = theme.ACCENT, size: int = 9,
                 hollow: bool = False, parent=None):
        super().__init__(parent)
        self._colour = QColor(colour)
        self._size = size
        self._hollow = hollow
        # A little wider than the diamond so it never touches the text next to
        # it, and fixed so a row of them lines up regardless of label length.
        self.setFixedSize(QSize(size + 8, size + 8))

    def set_colour(self, colour: str) -> None:
        self._colour = QColor(colour)
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        centre_x = self.width() / 2
        centre_y = self.height() / 2
        half = self._size / 2
        shape = QPolygonF([
            QPointF(centre_x, centre_y - half),
            QPointF(centre_x + half, centre_y),
            QPointF(centre_x, centre_y + half),
            QPointF(centre_x - half, centre_y),
        ])

        if self._hollow:
            pen = painter.pen()
            pen.setColor(self._colour)
            pen.setWidthF(1.4)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._colour)
        painter.drawPolygon(shape)
