"""Item pictures in lists, cached so a table does not decode them twice.

The pictures are the game's own icons, fetched once from DE's Public Export
and stored as PNG with their transparency intact (see `wft/data/pictures.py`).
They arrive as bytes and have to become pixmaps before anything can draw them:
a table of six hundred rows would decode six hundred times on every repaint
without a cache, and the same item appears on four different pages.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap

from ui import theme
from wft.data import db as dbq

#: How large a picture is drawn beside a row, as a share of the row height.
#: Measured off the references rather than chosen: AlecaFrame's list rows give
#: the icon about two thirds of their height, poe.ninja's denser rows about
#: seventy percent of a smaller row. Below half it stops being recognisable and
#: becomes a smudge; at full height it turns a list into a contact sheet.
ROW_SHARE = 0.68

#: The row height itself, in line heights. Two lines of text is what a row of
#: name-plus-detail needs, and it gives the icon something to be two thirds of.
ROW_LINES = 2.0

_cache: dict[tuple[str, str], QIcon] = {}


def clear() -> None:
    """Forget the cached pixmaps, after an import or a theme change."""
    _cache.clear()
    _blank.clear()


def icon(connection, kind: str, unique_name: str) -> QIcon | None:
    """The picture for one thing as an icon, or None if none is stored."""
    key = (kind, unique_name)
    if key in _cache:
        return _cache[key] or None
    data = dbq.thumbnail(connection, kind, unique_name)
    if not data:
        _cache[key] = None
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(data):
        _cache[key] = None
        return None
    made = QIcon(pixmap)
    _cache[key] = made
    return made


def row_height(widget) -> int:
    """How tall a row with a picture in it should be."""
    return int(widget.fontMetrics().height() * ROW_LINES)


def row_size(widget) -> QSize:
    """The icon size a list should ask for at the current text size."""
    side = int(row_height(widget) * ROW_SHARE)
    return QSize(side, side)


def decorate(connection, item, kind: str, unique_name: str, widget=None) -> bool:
    """Put the picture on a cell. Returns whether there was one."""
    made = icon(connection, kind, unique_name)
    if made is None:
        return False
    item.setIcon(made)
    return True

def best_icon(connection, part_unique: str | None,
              item_unique: str | None) -> QIcon | None:
    """The part's own picture, or its set's.

    A component often carries a generic blueprint texture in the export, and
    forty identical blueprint icons down a column say nothing. The set's icon
    is what makes such a column scannable.
    """
    if part_unique:
        found = icon(connection, "part", part_unique)
        if found is not None:
            return found
    if item_unique:
        return icon(connection, "item", item_unique)
    return None

_blank: dict[int, QIcon] = {}


def blank(widget) -> QIcon:
    """An empty icon of the right size, so rows without a picture still line up.

    Qt indents a row by its icon only when there is one, so a list where some
    things have pictures and some do not comes out ragged - and the ones
    without look like a rendering fault rather than a gap in the data.
    """
    side = row_size(widget).width()
    if side not in _blank:
        pixmap = QPixmap(side, side)
        pixmap.fill(Qt.transparent)
        _blank[side] = QIcon(pixmap)
    return _blank[side]


def decorate_or_space(connection, item, kind: str, unique_name: str,
                      widget) -> bool:
    """Put the picture on a cell, or the space where one would be."""
    if decorate(connection, item, kind, unique_name):
        return True
    item.setIcon(blank(widget))
    return False
