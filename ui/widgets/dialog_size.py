"""How big a dialog opens: from what it holds, never from a pixel count.

Four dialogs had `resize(760, 560)` and the like, numbers picked at the default
text size. At 20pt the same content needs 1.4 times the width and 1.4
times the height, and the fixed size gave the table the leftovers: the mastery
list showed 1.2 rows of 12, the set window's relic table had no height at all,
and the riven history was 1281px of table in 778px of dialog with its last
three columns neither visible nor reachable.

The size is now what the layout asks for plus whatever the wide tables need,
and never more than the screen can give. The tables inside have a minimum in
rows (`tables.keep_rows`), so a dialog cut down by a small screen takes it out
of the table's scrollbar and not out of the table's rows.
"""
from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QTableWidget

from ui import theme
from ui.widgets import tables

#: How much of the screen a dialog may take. Not all of it: a window that
#: covers the whole screen has no edge to grab, and the panels of the desktop
#: are inside `availableGeometry` only on some compositors.
SCREEN_SHARE_W = 0.92
SCREEN_SHARE_H = 0.88


def available_area(widget) -> QRect:
    """The part of the screen a dialog of `widget` may use.

    A function of its own so a test can answer for a screen that is not there:
    the offscreen platform has 800x800, which no real screen has.
    """
    screen = widget.screen() or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def open_at(dialog: QDialog, width_em: float, height_em: float) -> None:
    """Open `dialog` at a size given in ems of the text, capped to the screen.

    For a dialog whose size is a place to draw rather than a table to hold: the
    calibration picture wants room in proportion to the text around it, and
    1100x760 pixels was that room at 11pt only.
    """
    room = available_area(dialog)
    dialog.resize(min(theme.px(width_em), int(room.width() * SCREEN_SHARE_W)),
                  min(theme.px(height_em), int(room.height() * SCREEN_SHARE_H)))


def fit_dialog(dialog: QDialog, *, wide: tuple[QTableWidget, ...] = (),
               tall: QTableWidget | None = None, rows: int = 12) -> None:
    """Open `dialog` at the size its content asks for, capped to the screen.

    `wide` are the tables whose columns must all be reachable: the dialog is at
    least as wide as the widest of them wants to be. `tall` is a table that scrolls
    and takes the slack: the dialog is tall enough for `rows` of it (or all of
    them, if fewer) next to everything else.
    """
    layout = dialog.layout()
    layout.activate()
    want = dialog.sizeHint()
    width, height = want.width(), want.height()
    margins = layout.contentsMargins()
    for table in wide:
        width = max(width, tables.natural_width(table)
                    + margins.left() + margins.right())
    if tall is not None:
        shown = min(rows, max(tall.rowCount(), 1))
        # The layout counts the table at the larger of its size hint and its
        # minimum, so that is what the rows replace.
        counted = max(tall.sizeHint().height(), tall.minimumHeight())
        height += tables.rows_height(tall, shown) - counted
    room = available_area(dialog)
    dialog.resize(min(width, int(room.width() * SCREEN_SHARE_W)),
                  min(height, int(room.height() * SCREEN_SHARE_H)))
