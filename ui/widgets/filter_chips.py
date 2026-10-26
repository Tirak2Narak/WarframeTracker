"""Filter chip row, ported from the search page of an earlier attempt: a row
of toggle buttons, one of which is
always effectively "no filter".

Multiple category chips can be active at once (OR); picking "All" clears the
rest, and picking any other chip turns "All" off.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import QLayout, QSizePolicy, QToolButton, QWidget

ALL = "__all__"


class _Flow(QLayout):
    """A row of chips that breaks onto the next line instead of shrinking.

    The row was a QHBoxLayout, which distributes the shortfall over its
    children when there is not enough width: eleven category chips in a 1300
    pixel window came out as "W...s", "P...y", "S...y" - every label elided to
    its first and last letter, so the filter could not be read at all. A chip
    is a word or it is nothing.

    The Qt flow-layout pattern, kept to the part that is used here: every chip
    at the width it asks for, wrapped when the line is full, and a height that
    follows from the width so the page above can make room for a second line.
    """

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)

    # -- the five methods QLayout leaves to its subclass --------------------
    def addItem(self, item):
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    # -- height follows width ----------------------------------------------
    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._lay_out(QRect(0, 0, width, 0), place=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._lay_out(rect, place=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        """One chip wide. Anything more and the row could not wrap at all."""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _lay_out(self, rect: QRect, place: bool) -> int:
        margins = self.contentsMargins()
        field = rect.adjusted(margins.left(), margins.top(),
                              -margins.right(), -margins.bottom())
        x, y, line = field.x(), field.y(), 0
        gap = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > field.right() and line > 0:
                x, y, line = field.x(), y + line + gap, 0
            if place:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + gap
            line = max(line, hint.height())
        return y + line - rect.y() + margins.bottom()


class FilterChips(QWidget):
    """`options` is a list of (key, label) pairs. `ALL` is added automatically."""

    changed = Signal(set)  # active keys, or {ALL} meaning no filter

    def __init__(self, options: list[tuple[str, str]], all_label: str = "All", parent=None):
        super().__init__(parent)
        self._buttons: dict[str, QToolButton] = {}
        # Tall enough for however many lines the chips need, and no taller.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout = _Flow(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._add(layout, ALL, all_label, checked=True)
        for key, label in options:
            self._add(layout, key, label)

    def _add(self, layout: QLayout, key: str, label: str, checked: bool = False):
        button = QToolButton()
        button.setObjectName("FilterChip")
        button.setText(label)
        button.setCheckable(True)
        button.setChecked(checked)
        button.clicked.connect(lambda _checked, k=key: self._on_clicked(k))
        layout.addWidget(button)
        self._buttons[key] = button

    def _on_clicked(self, key: str):
        if key == ALL:
            for k, button in self._buttons.items():
                button.setChecked(k == ALL)
        else:
            self._buttons[ALL].setChecked(False)
            if not any(b.isChecked() for k, b in self._buttons.items() if k != ALL):
                self._buttons[ALL].setChecked(True)
        self.changed.emit(self.active())

    def active(self) -> set[str]:
        if self._buttons[ALL].isChecked():
            return {ALL}
        return {k for k, b in self._buttons.items() if k != ALL and b.isChecked()}
