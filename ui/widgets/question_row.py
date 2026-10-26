"""A question, in the same shape as everything else in the list.

The review was two kinds of row: questions as tall cards with radio buttons,
and accepted readings as compact rows with a picture, a count and a tick. In
one list that reads as two lists that happen to be stacked, so the second way of
working is used everywhere.

So a question is a row too — picture, what was read, what the scan thinks it
is, and why it is being asked. It opens into the full card when it is the row
being answered, and closes again when the next one is. One shape to scan, one
card at a time to answer.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets.accepted_row import THUMBNAIL_LINES, THUMBNAIL_MINIMUM
from ui.widgets.elided_label import ElidedLabel
from ui.widgets.tile_view import TilePicture


class QuestionRow(QWidget):
    """The closed form of a question: what was read, and why it is a question."""

    #: Somebody clicked the row rather than the picture.
    chosen = Signal()

    def __init__(self, reading: str, tiles: int, reason: str, guess: str,
                 answered: bool, count: int = 1,
                 evidence: bytes | None = None,
                 frame: bytes | None = None, bbox=None, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(theme.px(0.5), theme.px(0.25),
                               theme.px(0.5), theme.px(0.25))
        row.setSpacing(theme.px(0.6))

        thumbnail = max(THUMBNAIL_MINIMUM,
                        int(self.fontMetrics().height() * THUMBNAIL_LINES))
        if evidence:
            picture = TilePicture(evidence, frame, bbox,
                                  caption=f'read as "{reading}"')
            picture.show_thumbnail(thumbnail)
            row.addWidget(picture)
        else:
            blank = QLabel("—")
            blank.setAlignment(Qt.AlignCenter)
            blank.setFixedWidth(thumbnail + theme.px(0.5))
            blank.setStyleSheet(theme.text("body", theme.TEXT_FAINT))
            row.addWidget(blank)

        words = QVBoxLayout()
        words.setSpacing(1)
        # Elided, with the whole text as the tooltip: a long reading made the
        # closed row 1443px wide at 20pt and the list scrolled sideways in a
        # 1600x900 window.
        title = ElidedLabel(f'"{reading}"'
                            + (f"  ·  {tiles} tiles" if tiles > 1 else ""))
        title.setStyleSheet(theme.text("body", weight=600))
        words.addWidget(title)
        under = QLabel(reason)
        under.setWordWrap(True)
        under.setStyleSheet(theme.text("small", theme.TEXT_DIM))
        words.addWidget(under)
        holder = QWidget()
        holder.setLayout(words)
        holder.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
        row.addWidget(holder, 1)

        # What it will be recorded as if nothing is chosen. A question left
        # alone still writes something, and saying what makes leaving it alone
        # a decision rather than an omission.
        answer = ElidedLabel(guess or "—")
        answer.setStyleSheet(theme.text(
            "body", theme.GOOD if answered else theme.WARN, weight=600))
        row.addWidget(answer)

        # The number this row would write, shown closed as well as open. A
        # question about a stack count that does not show the count is a
        # question with its subject missing.
        amount = QLabel(f"×{count}")
        amount.setStyleSheet(theme.text("body", theme.TEXT_DIM, weight=600))
        amount.setMinimumWidth(self.fontMetrics().horizontalAdvance("×0000"))
        amount.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(amount)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self.chosen.emit()
        super().mouseReleaseEvent(event)
