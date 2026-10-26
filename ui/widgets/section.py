"""A card with a heading that can be folded away.

The Plan page has four sections and one of them is routinely the longest: a
collection that has been played for years holds dozens of complete sets. Left
open it pushes the two sections that actually need attention off the screen,
and truncating it to ten rows throws away most of the answer.

Folding solves both. Everything stays reachable, nothing is silently cut, and
the sections that need acting on are visible without scrolling. A section that
is already good news - things finished, nothing to do - starts folded; one that
names outstanding work starts open.

The header is a button rather than a label with a click handler, so it is
reachable by keyboard and announces itself as something that can be activated.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets.diamond import Diamond


class Section(QFrame):
    """A titled card whose body can be shown or hidden."""

    def __init__(self, title: str, note: str = "", colour: str = theme.ACCENT,
                 start_open: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        # The colour this section was given is its judgement; the left edge
        # says the same thing as the diamond beside the title, in the place
        # every other card in the application says it.
        theme.mark(self, theme.judgement_for(colour))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.card_margins())
        outer.setSpacing(theme.px(0.55))

        head = QHBoxLayout()
        head.setSpacing(2)
        head.addWidget(Diamond(colour))

        self.toggle = QPushButton(title)
        self.toggle.setObjectName("SectionToggle")
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(start_open)
        # The same heading the Plan page writes by hand: one function says what
        # a card heading looks like. A click target as tall as a table row, not
        # the 0.9 of a line that padding 0 gave.
        self.toggle.setStyleSheet(
            f"QPushButton#SectionToggle {{"
            f"  {theme.section_text(colour)}"
            f"  background: transparent; border: none;"
            f"  padding: {theme.px(0.2)}px 0px; text-align: left;"
            f"}}")
        self.toggle.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.toggle.toggled.connect(self._on_toggled)
        head.addWidget(self.toggle)

        self.hint = QLabel("")
        self.hint.setStyleSheet(theme.text("small", theme.TEXT_FAINT))
        head.addWidget(self.hint)
        head.addStretch(1)
        outer.addLayout(head)

        if note:
            caption = QLabel(note)
            caption.setObjectName("ViewSubtitle")
            caption.setWordWrap(True)
            outer.addWidget(caption)

        self.body_host = QWidget()
        self.body = QVBoxLayout(self.body_host)
        self.body.setContentsMargins(0, theme.px(0.3), 0, 0)
        self.body.setSpacing(theme.px(0.4))
        outer.addWidget(self.body_host)

        self._on_toggled(start_open)

    def _on_toggled(self, shown: bool):
        self.body_host.setVisible(shown)
        self.hint.setText("" if shown else "click to show")

    def add(self, widget: QWidget):
        self.body.addWidget(widget)
