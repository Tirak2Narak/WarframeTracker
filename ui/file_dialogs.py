"""File dialogs that survive a large text size.

The interface font is set on the whole application, which is what makes the
text size setting work everywhere — including in Qt's file dialog. That dialog
opens at a size chosen for the default font, so at 14pt and above its labels
collide, its columns crush together and the whole thing is unreadable until it
is dragged bigger by hand.

Nothing is wrong with the dialog. It is simply not being given the room the
font needs. These wrappers ask for it: the same calls, sized from the font
rather than from an assumption about it.

`DontUseNativeDialog` is deliberate. The size can only be set on a dialog this
process owns, and a platform dialog would ignore it — leaving exactly the
problem this exists to solve on whichever desktop happens to provide one.
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QFileDialog, QSplitter, QWidget

from ui import theme

#: Rough character cell counts the dialog needs to lay out without collisions.
#: Measured against the point size rather than fixed, because the whole problem
#: is a fixed size meeting a font that changed.
WIDTH_IN_POINTS = 78
HEIGHT_IN_POINTS = 46

#: Never smaller than this, however small the font gets.
MIN_WIDTH = 900
MIN_HEIGHT = 560

#: The width of the places list on the left, in ems of the text. Qt gave it
#: 52px whatever the font, and it wants 105px at 11pt, 123 at 14 and 159 at 20:
#: "Home", "Desktop" and "Documents" were cut to "in", "C" and "w" (audit B8).
PLACES_EM = 9


def _sized(dialog: QFileDialog) -> QFileDialog:
    points = QApplication.font().pointSize()
    if points <= 0:
        points = 10
    dialog.setOption(QFileDialog.DontUseNativeDialog, True)
    places = theme.px(PLACES_EM)
    # The places list is added to the width the files need: the detail view's
    # four columns want 1057px at 14pt and had 896 once the list took its 168.
    dialog.resize(max(MIN_WIDTH, points * WIDTH_IN_POINTS + places),
                  max(MIN_HEIGHT, points * HEIGHT_IN_POINTS))
    splitter = dialog.findChild(QSplitter, "splitter")
    sidebar = dialog.findChild(QWidget, "sidebar")
    if sidebar is not None:
        sidebar.setMinimumWidth(places)
    if splitter is not None:
        # The splitter keeps the share it was given at the first layout, so the
        # width is set on it as well as the minimum on the list.
        splitter.setSizes([places, max(1, dialog.width() - places)])
    return dialog


def open_file(parent, caption: str, directory: str = "",
              filters: str = "All files (*)") -> str:
    dialog = _sized(QFileDialog(parent, caption, directory, filters))
    dialog.setFileMode(QFileDialog.ExistingFile)
    if dialog.exec() and dialog.selectedFiles():
        return dialog.selectedFiles()[0]
    return ""


def save_file(parent, caption: str, suggested: str = "",
              filters: str = "All files (*)") -> str:
    dialog = _sized(QFileDialog(parent, caption, "", filters))
    dialog.setAcceptMode(QFileDialog.AcceptSave)
    dialog.setFileMode(QFileDialog.AnyFile)
    if suggested:
        dialog.selectFile(suggested)
    if dialog.exec() and dialog.selectedFiles():
        return dialog.selectedFiles()[0]
    return ""


def open_directory(parent, caption: str, directory: str = "") -> str:
    dialog = _sized(QFileDialog(parent, caption, directory))
    dialog.setFileMode(QFileDialog.Directory)
    dialog.setOption(QFileDialog.ShowDirsOnly, True)
    if dialog.exec() and dialog.selectedFiles():
        return dialog.selectedFiles()[0]
    return ""
