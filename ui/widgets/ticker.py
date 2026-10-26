"""A one-line label that scrolls when its text is wider than the room it has.

The status line grew past the width of a narrow window once it carried three
ages. Cutting it would hide exactly the part that says when to import
again, so it runs past slowly, right to left, as a seamless band: the text, a
gap, the text again. When it fits it stands still at the left, like a plain
label. The timer only runs while the text overflows and the widget is shown, so
a fitting line costs nothing.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QTimer, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QSizePolicy


class TickerLabel(QLabel):
    #: Speed in pixels per second, and how often the position is advanced.
    SPEED = 30.0
    INTERVAL_MS = 33
    #: The empty stretch between the end of the text and its repeat, in ems.
    GAP_EMS = 3

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self._offset = 0.0
        self._hovered = False
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setAttribute(Qt.WA_Hover, True)
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self.setText(text)

    # -- state ---------------------------------------------------------------
    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        self._full = text
        self._offset = 0.0
        self.setToolTip(text)
        super().setText(text)
        self._sync()

    def fullText(self) -> str:  # noqa: N802 - Qt-style name
        return self._full

    def offset(self) -> float:
        return self._offset

    def is_scrolling(self) -> bool:
        return self._timer.isActive()

    def overflows(self) -> bool:
        return self.fontMetrics().horizontalAdvance(self._full) \
            > self.contentsRect().width()

    def _band(self) -> float:
        """Width of one text plus its gap: where the scroll wraps round."""
        metrics = self.fontMetrics()
        return metrics.horizontalAdvance(self._full) \
            + self.GAP_EMS * metrics.horizontalAdvance("M")

    def _sync(self) -> None:
        """Run the timer exactly while there is something to scroll."""
        if self.overflows() and self.isVisible():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._offset = 0.0
        self.update()

    def _tick(self) -> None:
        if self._hovered:
            return
        self._offset = (self._offset + self.SPEED * self.INTERVAL_MS / 1000.0) \
            % self._band()
        self.update()

    # -- Qt hooks ------------------------------------------------------------
    def minimumSizeHint(self):  # noqa: N802 - Qt override
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._sync()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._timer.stop()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        # A font or theme change alters the text's width.
        if event.type() in (event.Type.FontChange, event.Type.StyleChange):
            self._sync()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hovered = True
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hovered = False
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        painter.setClipRect(rect)
        flags = int(Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine)
        if not self.overflows():
            painter.drawText(rect, flags, self._full)
            return
        band = self._band()
        x = rect.left() - self._offset
        while x < rect.right():
            painter.drawText(QRect(round(x), rect.top(), 100000, rect.height()),
                             flags, self._full)
            x += band
