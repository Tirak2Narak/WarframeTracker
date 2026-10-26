"""What past imports wrote, and the way to take one back.

The inventory tables hold one number per thing: the latest. That answered "what
do I have" and nothing else — not "what did that import change", not "was that
number always wrong", and not "take it back". The one wrong number this tool
has produced, a stack of 12 stored as 2, was invisible for exactly that reason:
there was no 12 anywhere to compare against.

Rows are ordered by how far each moved. That is the order a mistake is found
in — a count that jumped by ten is worth a second look, one that moved by one
is the tool working.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidgetItem, QVBoxLayout,
)

from ui import theme
from ui.widgets.tables import fit_height, simple_table
from wft.data import db as dbq

#: Rows shown for one import. Enough to see the shape of what it did; the
#: journal keeps all of them either way.
ROW_LIMIT = 40

#: Imports offered in the picker. Deep enough to reach past a bad run and the
#: corrections after it - twelve was a fortnight of daily scanning and then a
#: cliff, with older imports neither visible nor reversible.
HISTORY_DEPTH = 50


class ImportHistory(QFrame):
    """A picker for past imports, what each changed, and an undo."""

    changed = Signal()

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.conn = connection
        self.setObjectName("Card")
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.7))

        heading = QLabel("What past imports wrote")
        heading.setObjectName("SectionHeader")
        layout.addWidget(heading)

        row = QHBoxLayout()
        self.picker = QComboBox()
        self.picker.currentIndexChanged.connect(self._show_selected)
        # May be narrower than its longest entry: its minimum was that entry's
        # whole text (754px at 20pt) and the Import page ran sideways in a
        # 1600x900 window. The entries are full in the open list.
        self.picker.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.picker.setMinimumContentsLength(24)
        row.addWidget(self.picker, 1)
        self.undo_button = QPushButton("Take this import back")
        self.undo_button.clicked.connect(self._undo)
        row.addWidget(self.undo_button)
        layout.addLayout(row)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.note)

        # The shared table, fitted to the rows it holds: the page it
        # sits on scrolls. It used to be given "room for the header and a
        # handful of rows" as a pixel minimum, because given only what was
        # left over it showed the header and no row.
        self.table = simple_table(
            ["", "Name", "Read as", "Was", "Now", "Change"], 0, stretch=1,
            wrap=True)
        layout.addWidget(self.table)

    def refresh(self):
        self.picker.blockSignals(True)
        self.picker.clear()
        self._imports = dbq.recent_imports(self.conn, HISTORY_DEPTH)
        for entry in self._imports:
            label = (f"#{entry['id']}  ·  {entry['written_at']}  ·  "
                     f"{entry['changed']} change(s) of {entry['rows']}")
            if entry["undone_at"]:
                label += "   (taken back)"
            self.picker.addItem(label, entry["id"])
        self.picker.blockSignals(False)
        self.setVisible(bool(self._imports))
        if self._imports:
            self._show_selected()

    def _current(self):
        index = self.picker.currentIndex()
        if 0 <= index < len(self._imports):
            return self._imports[index]
        return None

    def _show_selected(self):
        entry = self._current()
        if entry is None:
            return
        undone = bool(entry["undone_at"])
        self.undo_button.setEnabled(not undone)
        source = entry["source"] or "—"
        self.note.setText(
            f"{entry['tiles'] or '?'} tile(s) read, {entry['questions'] or 0} "
            f"needed review. From {source}."
            + ("  Already taken back." if undone else ""))

        rows = dbq.import_rows(self.conn, entry["id"])[:ROW_LIMIT]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            before = row["before"] or 0
            after = row["after"] or 0
            self.table.setItem(index, 0, QTableWidgetItem(row["kind"]))
            self.table.setItem(index, 1, QTableWidgetItem(
                dbq.name_for(self.conn, row["kind"], row["unique_name"])
                if row["kind"] != "mastery"
                else dbq.name_for(self.conn, "item", row["unique_name"])))
            # What OCR read, beside what it was taken for. A number that turns
            # out wrong is always followed by "what did it think it saw", and
            # the answer used to be gone by then.
            reading = QTableWidgetItem(row["reading"] or "")
            reading.setForeground(theme.qcolor(theme.TEXT_DIM))
            self.table.setItem(index, 2, reading)
            self.table.setItem(index, 3, QTableWidgetItem(str(before)))
            self.table.setItem(index, 4, QTableWidgetItem(str(after)))
            delta = after - before
            cell = QTableWidgetItem("—" if not delta else f"{delta:+d}")
            # Only the size of the move is coloured, not its direction. A drop
            # is as often a correction as a loss, and calling one of them red
            # would be the tool having an opinion it has no basis for.
            if abs(delta) >= 5:
                cell.setForeground(theme.qcolor(theme.WARN))
            self.table.setItem(index, 5, cell)
        fit_height(self.table)

    def _undo(self):
        entry = self._current()
        if entry is None:
            return
        reply = QMessageBox.question(
            self, f"Take import #{entry['id']} back?",
            f"Every number this import wrote goes back to what stood there "
            f"before it — {entry['changed']} of {entry['rows']} rows actually "
            f"changed.\n\nAnything altered since, by hand or by a later "
            f"import, is left alone.")
        if reply != QMessageBox.Yes:
            return
        restored = dbq.undo_import(self.conn, entry["id"])
        QMessageBox.information(
            self, "Import taken back",
            f"{restored} row(s) put back as they were.")
        self.refresh()
        self.changed.emit()
