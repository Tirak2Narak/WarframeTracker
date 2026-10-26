"""Confirming what the profile says has been mastered.

The flag latches: a scan sets it and no scan clears it, because the points
survive the sale. A hundred rows arriving from a source outside the tool is
exactly the case that should be looked at before it is written, and it is
also the case where looking is cheap - the list is names and numbers, and a
wrong one stands out.

Everything is checked by default. The profile is Warframe's own record of what
the account levelled, which is better evidence than anything else here has;
unchecking is for the rare item the user knows is wrong, not the default
posture.
"""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets import tables
from ui.widgets.dialog_size import fit_dialog
from ui.widgets.tables import keep_rows, simple_table
from wft.data import db as dbq


#: Rows of the list that stay visible however much text is above it.
MIN_ROWS = 6


class MasteryImport(QDialog):
    """The mastery the profile knows about and this database does not."""

    def __init__(self, connection, found: dict, parent=None):
        super().__init__(parent)
        self.conn = connection
        self.rows = found["mastered"]
        self.setWindowTitle("Mastery from your profile")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.7))

        # The total, not only the new ones. Three imports produced "202",
        # "309" and "227" and each was a different quantity — a running total,
        # a running total, and a count of additions — with nothing on screen
        # saying which. A number that changes for reasons the screen does not
        # explain reads as a bug in the import.
        total = len(self.rows) + int(found.get("already", 0))
        heading = QLabel(
            f"{len(self.rows)} new — {total} mastered in total")
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)

        note = QLabel(
            f"Warframe\u2019s profile records the affinity earned on every item "
            f"the account has levelled, whether or not it is still owned \u2014 "
            f"which is why this finds what a scan of the inventory cannot. "
            f"The profile lists {total} item(s) at their rank cap. "
            f"{found['already']} of those are already marked here, so "
            f"{len(self.rows)} would be added. {found['short']} more have not "
            f"reached their cap yet and are left alone.\n\n"
            f"Mastery cannot be un-set by a later scan, so it is worth a look "
            f"before writing.")
        note.setWordWrap(True)
        note.setObjectName("ViewSubtitle")
        layout.addWidget(note)

        # The shared table, not fitted: the list can be a hundred rows
        # long and the dialog is the page. It keeps six rows whatever the note
        # above it takes - at 20pt the note took the dialog and the list had
        # 88px, one row of twelve.
        table = simple_table(["", "Item", "Affinity earned"], len(self.rows),
                             stretch=1, fit=False)

        self.boxes = []
        for row, (_unique, name, xp) in enumerate(self.rows):
            box = QCheckBox()
            box.setChecked(True)
            holder = QWidget()
            inner = QHBoxLayout(holder)
            inner.setContentsMargins(theme.px(0.5), 0, 0, 0)
            inner.addWidget(box)
            inner.addStretch(1)
            table.setCellWidget(row, 0, holder)
            table.setItem(row, 1, QTableWidgetItem(name))
            value = QTableWidgetItem(f"{xp:,}")
            value.setForeground(QColor(theme.TEXT_DIM))
            table.setItem(row, 2, value)
            self.boxes.append(box)
        tables.check_rows(
            table, lambda row: self.boxes[row] if row < len(self.boxes) else None)
        keep_rows(table, MIN_ROWS)
        layout.addWidget(table, 1)

        buttons = QHBoxLayout()
        none_btn = QPushButton("Uncheck all")
        none_btn.clicked.connect(
            lambda: [box.setChecked(False) for box in self.boxes])
        buttons.addWidget(none_btn)
        all_btn = QPushButton("Check all")
        all_btn.clicked.connect(
            lambda: [box.setChecked(True) for box in self.boxes])
        buttons.addWidget(all_btn)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.write_btn = QPushButton("Mark as mastered")
        self.write_btn.setObjectName("Primary")
        self.write_btn.clicked.connect(self._write)
        buttons.addWidget(self.write_btn)
        layout.addLayout(buttons)
        fit_dialog(self, wide=(table,), tall=table)

    def _write(self):
        for box, (unique, _name, _xp) in zip(self.boxes, self.rows):
            if box.isChecked():
                dbq.set_item_mastered(self.conn, unique, source="profile")
        self.accept()
