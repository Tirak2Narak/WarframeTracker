"""Marking the reward row on a real screen.

The reward regions depend on the screen, the aspect ratio and the interface
scale. Only the person in front of the game can say where the row is, and all it
takes is dragging a box around the first card and then nudging it into place.

What is marked, all in pixels of the picture:

* the first (leftmost) reward card, a red frame: drawn with the mouse, then
  moved, resized and checked against the picture one pixel at a time;
* the gap between two cards, a number of its own: the cards are not edge to
  edge, and a pitch taken to be the width put the third card of three 30 px off
  at 4K;
* the name band of that first card, a green frame, which belongs to the card
  (it moves with it) and is fitted by its own top and height;
* the other cards of the row, pale orange, from the card, the gap and the number
  of rewards: the row is centred, so the rest follows from these.

The keys work like the palette grid's (`ui/overlay/helper.py`): arrows move by 1
pixel, with Ctrl by 10, with Shift they change the size.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy

#: Smallest card or band the keys can shrink to, in picture pixels.
MIN_SIZE = 4

#: What a loupe shows: this many picture pixels square, drawn at LOUPE_ZOOM.
LOUPE_SPAN = 27
LOUPE_ZOOM = 4

CARD = "card"
BAND = "band"


@dataclass
class RowMarks:
    """The marks, in pixels of the picture. No Qt: the tests drive it directly."""

    #: Picture size in pixels; 0 while there is no picture.
    width: int = 0
    height: int = 0
    #: The first card: x, y, width, height. None until one is drawn.
    card: tuple[int, int, int, int] | None = None
    gap: int = 0
    count: int = 4
    #: The name band of the first card, as its top below the card's top and its
    #: height, so it follows the card.
    band_dy: int = 0
    band_height: int = 0
    mode: str = CARD

    def set_card(self, x: int, y: int, w: int, h: int, band: tuple[float, float]) -> None:
        """A newly drawn card; the band is derived from it as fractions of the
        card's height (top, bottom) and can be fitted afterwards."""
        self.card = (x, y, max(MIN_SIZE, w), max(MIN_SIZE, h))
        self.band_dy = round(band[0] * h)
        self.band_height = max(MIN_SIZE, round((band[1] - band[0]) * h))
        self.mode = CARD

    def clear(self) -> None:
        self.card = None
        self.mode = CARD

    @property
    def span(self) -> int:
        """Width of the whole row, in pixels."""
        return self.count * self.card[2] + (self.count - 1) * self.gap

    def card_rects(self) -> list[tuple[int, int, int, int]]:
        """Every card of the row; the first is the marked one."""
        if self.card is None:
            return []
        x, y, w, h = self.card
        return [(x + k * (w + self.gap), y, w, h) for k in range(self.count)]

    def band_rect(self) -> tuple[int, int, int, int] | None:
        """The name band of the first card."""
        if self.card is None:
            return None
        x, y, w, _h = self.card
        return (x, y + self.band_dy, w, self.band_height)

    def nudge(self, dx: int, dy: int, resize: bool = False) -> None:
        """Move by (dx, dy) pixels, or with `resize` change the size by it.

        On the card: arrows move it, with `resize` left/right change the width
        and up/down the height. On the band: up/down move it relative to the
        card (left/right do nothing: it is as wide as the card), with `resize` up
        and down change its height.
        """
        if self.card is None:
            return
        x, y, w, h = self.card
        if self.mode == CARD:
            if resize:
                w, h = max(MIN_SIZE, w + dx), max(MIN_SIZE, h + dy)
            else:
                x, y = max(0, x + dx), max(0, y + dy)
            self.card = (x, y, w, h)
        elif resize:
            self.band_height = max(MIN_SIZE, self.band_height + dy)
        else:
            self.band_dy += dy

    def nudge_gap(self, delta: int) -> None:
        self.gap = max(0, self.gap + delta)

    def toggle_mode(self) -> None:
        if self.card is not None:
            self.mode = BAND if self.mode == CARD else CARD

    def fractions(self) -> dict[str, float] | None:
        """The values the regions are stored as, fractions of the picture."""
        if self.card is None or not self.width or not self.height:
            return None
        x, y, w, h = self.card
        return {
            "tile_top": y / self.height,
            "tile_height": h / self.height,
            "tile_width": w / self.width,
            "tile_gap": self.gap / self.width,
            "row_centre_x": (x + self.span / 2) / self.width,
            "name_top": (y + self.band_dy) / self.height,
            "name_height": self.band_height / self.height,
        }

    def from_fractions(self, values: dict[str, float], count: int) -> None:
        """The reverse, for starting from the regions in effect."""
        w = round(values["tile_width"] * self.width)
        gap = round(values["tile_gap"] * self.width)
        span = count * w + (count - 1) * gap
        self.count, self.gap = count, gap
        x = round(values["row_centre_x"] * self.width - span / 2)
        y, h = round(values["tile_top"] * self.height), round(values["tile_height"] * self.height)
        self.card = (x, y, w, h)
        self.band_dy = round(values["name_top"] * self.height) - y
        self.band_height = round(values["name_height"] * self.height)
        self.mode = CARD


class RegionPicker(QLabel):
    """A picture with the marks of the reward row on it, drawn and nudged."""

    #: The first card was drawn with the mouse: window fractions x, y, w, h.
    picked = Signal(float, float, float, float)
    #: Any change of the marks (mouse or keys).
    changed = Signal()

    def __init__(self, image: QPixmap, parent=None):
        super().__init__(parent)
        self._source = image
        self.marks = RowMarks()
        self._rect: QRect | None = None
        self._origin: QPoint | None = None
        #: Said on the empty surface instead of a dark rectangle: a newcomer
        #: opened the dialog first, saw nothing, and could not tell where a
        #: picture was meant to come from.
        self.placeholder = ""
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        # Pixels, on purpose: this is a floor for a picture, not for text. In
        # ems it was 853x480 at 20pt and the dialog, capped to the screen,
        # overlapped its own rows in a 1600x900 window.
        self.setMinimumSize(480, 270)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    # -- geometry ------------------------------------------------------------
    def _shown(self) -> QRect:
        """Where the scaled picture sits inside the widget.

        The label centres the pixmap, so a click has to be translated back
        through both the offset and the scale before it means anything.
        """
        if self._source.isNull():
            return QRect()
        scaled = self._source.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRect(QPoint((self.width() - scaled.width()) // 2,
                            (self.height() - scaled.height()) // 2), scaled)

    def sync_size(self) -> None:
        """Take the picture's size into the marks (after the picture changed)."""
        self.marks.width = self._source.width()
        self.marks.height = self._source.height()

    def _to_picture(self, point: QPoint) -> QPointF | None:
        shown = self._shown()
        if shown.isEmpty():
            return None
        return QPointF((point.x() - shown.x()) * self._source.width() / shown.width(),
                       (point.y() - shown.y()) * self._source.height() / shown.height())

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101318"))
        shown = self._shown()
        if shown.isEmpty() and self.placeholder:
            # The picture area is dark whatever the theme, so the text is a
            # fixed light grey rather than a theme colour.
            painter.setPen(QColor("#a9b1c0"))
            painter.drawText(self.rect().adjusted(24, 24, -24, -24),
                             Qt.AlignCenter | Qt.TextWordWrap, self.placeholder)
        if not shown.isEmpty():
            painter.drawPixmap(shown, self._source)
            scale = shown.width() / self._source.width()
            painter.save()
            painter.translate(shown.x(), shown.y())
            painter.scale(scale, scale)
            self._paint_marks(painter, cosmetic=True)
            painter.restore()
            if self.marks.card is not None:
                self._paint_loupes(painter)
        drawn = self._rect
        if drawn and not drawn.isEmpty():
            pen = QPen(QColor("#ff4d4d"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(QColor(255, 77, 77, 40))
            painter.drawRect(drawn)
        painter.end()

    def _paint_marks(self, painter: QPainter, cosmetic: bool) -> None:
        """The cards and the band, in picture pixels (the painter is scaled)."""
        marks = self.marks
        if marks.card is None:
            return
        rects = marks.card_rects()

        def draw(rect, colour, fill, width, dash=False):
            pen = QPen(QColor(colour))
            pen.setCosmetic(cosmetic)
            pen.setWidth(width)
            if dash:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(fill)
            x, y, w, h = rect
            painter.drawRect(QRectF(x, y, w, h))

        for rect in rects[1:]:
            draw(rect, "#ffb366", QColor(255, 179, 102, 30), 2)
        draw(rects[0], "#ff3b3b", QColor(255, 59, 59, 28),
             3 if marks.mode == CARD else 2)
        band = marks.band_rect()
        draw(band, "#3ecf8e", QColor(62, 207, 142, 60),
             3 if marks.mode == BAND else 2, dash=marks.mode != BAND)

    def _loupe_targets(self) -> list[tuple[QRect, QPointF]]:
        """The small magnifiers along the bottom: where each looks, in picture
        pixels. The corner of the card, the gap between the first two cards and
        the corner of the band: the three things a pixel decides."""
        marks = self.marks
        x, y, w, h = marks.card
        band = marks.band_rect()
        centres = [QPointF(x, y), QPointF(x + w + marks.gap / 2, y + 30),
                   QPointF(band[0], band[1])]
        size = LOUPE_SPAN * LOUPE_ZOOM
        top = self.height() - size - 6
        left = (self.width() - 3 * size - 2 * 6) // 2
        return [(QRect(left + i * (size + 6), top, size, size), c)
                for i, c in enumerate(centres)]

    def _paint_loupes(self, painter: QPainter) -> None:
        for box, centre in self._loupe_targets():
            painter.save()
            painter.setClipRect(box)
            painter.fillRect(box, QColor("#101318"))
            painter.translate(box.center().x() + 0.5, box.center().y() + 0.5)
            painter.scale(LOUPE_ZOOM, LOUPE_ZOOM)
            painter.translate(-centre.x(), -centre.y())
            painter.drawPixmap(0, 0, self._source)
            self._paint_marks(painter, cosmetic=True)
            painter.restore()
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(box.adjusted(0, 0, -1, -1))

    # -- the mouse -----------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        self.setFocus()
        self._origin = event.position().toPoint()
        self._rect = QRect(self._origin, self._origin)
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        if self._origin is not None:
            self._rect = QRect(self._origin, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        origin, self._origin = self._origin, None
        rect, self._rect = self._rect, None
        shown = self._shown()
        self.update()
        if rect is None or shown.isEmpty():
            return
        box = rect.intersected(shown)
        if box.width() < 4 or box.height() < 4:
            # A click, not a drag: it chooses what the keys adjust.
            if origin is not None:
                self._choose_at(self._to_picture(origin))
            return
        self.picked.emit((box.x() - shown.x()) / shown.width(),
                         (box.y() - shown.y()) / shown.height(),
                         box.width() / shown.width(), box.height() / shown.height())

    def _choose_at(self, point: QPointF | None) -> None:
        """A click on the band selects the band, on a card selects the card."""
        marks = self.marks
        if point is None or marks.card is None:
            return
        inside = lambda r: r[0] <= point.x() < r[0] + r[2] and r[1] <= point.y() < r[1] + r[3]  # noqa: E731
        if inside(marks.band_rect()):
            mode = BAND
        elif any(inside(r) for r in marks.card_rects()):
            mode = CARD
        else:
            return
        if mode != marks.mode:
            marks.mode = mode
            self._marks_changed()

    # -- the marks, from outside ---------------------------------------------
    def set_card_fractions(self, x: float, y: float, w: float, h: float,
                           band: tuple[float, float]) -> None:
        """Put the first card where fractions of the picture say, in pixels."""
        self.sync_size()
        px, py = round(x * self.marks.width), round(y * self.marks.height)
        self.marks.set_card(px, py, round(w * self.marks.width),
                            round(h * self.marks.height), band)
        self._marks_changed()

    def clear_selection(self) -> None:
        self._rect = None
        self.sync_size()
        self.marks.clear()
        self.update()

    def _marks_changed(self) -> None:
        self.update()
        self.changed.emit()

    # -- the keys ------------------------------------------------------------
    def focusNextPrevChild(self, next_: bool) -> bool:  # noqa: N802 - Qt override
        """Tab and Shift+Tab switch between the red card and the green band
        while a card is marked; Ctrl+Tab (below) moves on to the next control."""
        if self.marks.card is None:
            return super().focusNextPrevChild(next_)
        self.marks.toggle_mode()
        self._marks_changed()
        return True

    def keyPressEvent(self, event):  # noqa: N802 - Qt override
        key, mods = event.key(), event.modifiers()
        if key in (Qt.Key_Tab, Qt.Key_Backtab) and mods & Qt.ControlModifier:
            super().focusNextPrevChild(not mods & Qt.ShiftModifier)
            return
        step = 10 if mods & Qt.ControlModifier else 1
        delta = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0),
                 Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}.get(key)
        if delta is None or self.marks.card is None:
            super().keyPressEvent(event)
            return
        if mods & Qt.AltModifier:
            if delta[0]:
                self.marks.nudge_gap(delta[0])
                self._marks_changed()
            return
        self.marks.nudge(*delta, resize=bool(mods & Qt.ShiftModifier))
        self._marks_changed()
