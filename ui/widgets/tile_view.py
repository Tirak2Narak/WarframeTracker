"""The tile crop, clickable, and the picture it opens.

A thumbnail is enough to see that a tile is a sword rather than a helmet, and
not enough to see why a reading went wrong. The answer is usually next door — a
neighbour bleeding into the crop, a dialog over the shelf, a tile half off
screen — so the picture worth opening is the whole frame with the tile ringed,
which is what the previous tool did.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

#: Fraction of the frame width the ring is drawn at, with a floor so it stays
#: visible on a small picture. Taken from the previous tool, which used 0.5%.
RING_WIDTH = 0.004
RING_MINIMUM = 3

#: How much of the screen the opened picture may take.
SCREEN_SHARE = 0.85


class TilePicture(QLabel):
    """A tile thumbnail that opens its source frame when clicked."""

    def __init__(self, evidence: bytes, frame: bytes | None = None,
                 bbox: tuple[int, int, int, int] | None = None,
                 caption: str = "", parent=None):
        super().__init__(parent)
        self.evidence = evidence
        self.frame = frame
        self.bbox = bbox
        self.caption = caption
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to see where this came from"
                        if frame else "Click to see this tile full size")

    def show_thumbnail(self, height: int) -> None:
        """Show the tile `height` pixels tall, as wide as that makes it.

        The width is the picture's own plus a margin in ems. Both review rows
        gave the label the height plus 8px, but a tile is wider than it is
        tall: the label was 259 of the 301px its picture is at 20pt and 184 of
        211 at 14pt, in every row.
        """
        from ui import theme

        pixmap = QPixmap()
        pixmap.loadFromData(self.evidence)
        scaled = pixmap.scaledToHeight(height, Qt.SmoothTransformation)
        self.setPixmap(scaled)
        self.setFixedWidth(scaled.width() + theme.px(0.5))

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            show_source(self, self.evidence, self.frame, self.bbox, self.caption)
        super().mouseReleaseEvent(event)


def _ringed(frame: bytes, bbox) -> QPixmap | None:
    image = QImage.fromData(frame)
    if image.isNull():
        return None
    pixmap = QPixmap.fromImage(image)
    if bbox:
        painter = QPainter(pixmap)
        pen = QPen(QColor("#ff2d2d"))
        pen.setWidth(max(RING_MINIMUM, int(pixmap.width() * RING_WIDTH)))
        painter.setPen(pen)
        left, top, right, bottom = bbox
        painter.drawRect(left, top, right - left, bottom - top)
        painter.end()
    return pixmap


def show_source(parent, evidence: bytes | None, frame: bytes | None = None,
                bbox=None, caption: str = "") -> None:
    """Open the frame a tile was cut from, with the tile ringed.

    Falls back to the crop alone when the frame is not available, which is what
    a result loaded from an older scan looks like.
    """
    pixmap = _ringed(frame, bbox) if frame else None
    if pixmap is None and evidence:
        image = QImage.fromData(evidence)
        if not image.isNull():
            pixmap = QPixmap.fromImage(image)
    if pixmap is None:
        return

    screen = QGuiApplication.primaryScreen()
    available = screen.availableGeometry() if screen else None
    if available:
        pixmap = pixmap.scaled(
            int(available.width() * SCREEN_SHARE),
            int(available.height() * SCREEN_SHARE),
            Qt.KeepAspectRatio, Qt.SmoothTransformation)

    dialog = QDialog(parent)
    dialog.setWindowTitle(caption or "Where this came from")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    if caption:
        heading = QLabel(f"  {caption}")
        heading.setObjectName("SectionHeader")
        layout.addWidget(heading)
    picture = QLabel()
    picture.setPixmap(pixmap)
    picture.setAlignment(Qt.AlignCenter)
    layout.addWidget(picture)
    close = QPushButton("Close  (Esc)")
    close.clicked.connect(dialog.accept)
    layout.addWidget(close)
    dialog.exec()
