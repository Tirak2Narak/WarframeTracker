"""A one-line label that shortens its middle instead of running off the page.

The footer of the Settings page shows where the configuration lives, and that
is a path: with the checkout under a long directory it was 2374px of unbroken
text on a page 1500px wide, and the label simply ended at the edge. A path has no spaces to wrap at, so wrapping cannot help;
the middle is the part a reader can do without - the start says which disk and
the end says which file. The whole text is the tooltip.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QSizePolicy


class ElidedLabel(QLabel):
    """Shows `text` on one line, elided in the middle when it does not fit."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        # Ignored horizontally: the label takes the width it is given and
        # never asks for the width of the full text.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        self._full = text
        self.setToolTip(text)
        super().setText(text)

    def fullText(self) -> str:  # noqa: N802 - Qt-style name
        return self._full

    def shown_text(self) -> str:
        """What is drawn at the current width."""
        return self.fontMetrics().elidedText(
            self._full, Qt.ElideMiddle, max(0, self.contentsRect().width()))

    def minimumSizeHint(self):  # noqa: N802 - Qt override
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.contentsRect(),
                         int(self.alignment()) | Qt.AlignVCenter,
                         self.shown_text())
