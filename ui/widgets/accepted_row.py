"""One reading the scan was sure of, laid out so it can be checked at a glance.

Confident is not the same as correct, and the errors that hurt are the ones
nothing asked about: "Gunsen Prime Blueprint" read as "Gunse Blueprint" matched
the non-Prime item at 96.8 with a clear lead and went into the inventory as a
complete set ready to build.

So every reading gets a row, and every row carries the four things needed to
judge it without leaving the page: the tile, the name it was matched to, the
text that was actually read, and the count. Every tile has to be visible.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets.elided_label import ElidedLabel
from ui.widgets.tile_view import TilePicture

#: How tall the tile thumbnail is drawn, in line heights. Big enough to
#: recognise the artwork and read the printed name on it, small enough that a
#: couple of hundred rows are a list rather than a slideshow.
#:
#: Measured against the text rather than fixed in pixels: at 20pt
#: the row is a hundred pixels tall whatever the picture does, so a 64px
#: thumbnail simply floated in space with the row no shorter for it.
#:
#: Raised again because they were too small: this is a picture somebody is
#: checking a reading against, not a bullet point beside a name. And doubled
#: again on the same argument, against real captures: a relic's artwork is
#: the same lump of metal whatever the relic, so the printed name under it is
#: what separates "Neo D9" from "Neo D10" - and at 72px that caption is four
#: pixels tall.
#:
#: The picture behind it is 320px wide (`EVIDENCE_WIDTH` in the scan), so this
#: still scales down. Going higher would start inventing detail.
THUMBNAIL_LINES = 6.8
THUMBNAIL_MINIMUM = 144

#: Below this the score is worth pointing at. Above it, a wrong match is a
#: near-perfect reading of the wrong thing, which the reading column shows
#: better than any number.
SCORE_WORTH_NOTING = 95.0


class AcceptedRow(QWidget):
    """A checkable row: keep it, correct its count, or open it for a second look."""

    #: The user wants to answer this one properly after all.
    reopened = Signal()

    def __init__(self, name: str, reading: str, tiles: int, score: float,
                 count: int, evidence: bytes | None = None,
                 frame: bytes | None = None, bbox=None, parent=None):
        super().__init__(parent)
        self.name = name
        self._build_ui(name, reading, tiles, score, count, evidence, frame, bbox)

    def _build_ui(self, name, reading, tiles, score, count, evidence, frame, bbox):  # noqa: PLR0913
        row = QHBoxLayout(self)
        row.setContentsMargins(theme.px(0.7), theme.px(0.3),
                               theme.px(0.7), theme.px(0.3))
        row.setSpacing(theme.px(0.8))
        thumbnail = max(THUMBNAIL_MINIMUM,
                        int(self.fontMetrics().height() * THUMBNAIL_LINES))

        # Tick, count and the way out, all before the picture. They used to sit
        # at the far end of a row as wide as the window: the user read the tile
        # on the left, then had to cross fifteen hundred pixels to see the
        # number it was checked against. Now the eyes stay on one side of the
        # screen.
        self.keep = QCheckBox()
        self.keep.setChecked(True)
        self.keep.setToolTip("Unticked readings are not written")
        row.addWidget(self.keep)

        # The count, here rather than two pages away. A wrong badge
        # reading had nowhere to be corrected on the row itself, with no way
        # to say "stop, that is wrong, it is only 1".
        self.count = QSpinBox()
        # Five figures because `parse_badge` accepts up to 99999, and a range
        # narrower than what can be read clamps silently.
        self.count.setRange(0, 99999)
        self.count.setValue(max(0, count))
        #: What the scan counted, so a correction can be told from agreement.
        self._scanned = max(0, count)
        self.count.setToolTip("How many of these you actually have")
        # Fixed at what five figures and the buttons need at this text size:
        # 84px was under the minimum at 11, 14 and 20pt (100, 125 and 174px),
        # which left the field's buttons as slits in all 208 rows.
        self.count.setFixedWidth(self.count.minimumSizeHint().width())
        row.addWidget(self.count)

        reopen = QPushButton("Wrong?")
        reopen.setToolTip("Show the alternatives for this reading")
        reopen.clicked.connect(self.reopened.emit)
        row.addWidget(reopen)

        if evidence:
            picture = TilePicture(evidence, frame, bbox, caption=name)
            picture.show_thumbnail(thumbnail)
            row.addWidget(picture)
        else:
            blank = QLabel("—")
            blank.setAlignment(Qt.AlignCenter)
            blank.setFixedWidth(thumbnail + theme.px(0.5))
            blank.setStyleSheet(f"color: {theme.TEXT_FAINT};")
            row.addWidget(blank)

        words = QVBoxLayout()
        words.setSpacing(1)
        # Shortened, not widened: a long name made the row 1474px wide at 20pt
        # and the list scrolled sideways in a 1600x900 window. The whole name
        # is the tooltip.
        title = ElidedLabel(name)
        title.setStyleSheet("font-weight: 600;")
        words.addWidget(title)

        # The reading beside the name is what makes a row checkable. A name on
        # its own never is: it is what the tool believes, not what it saw.
        detail = f'read "{reading}"'
        if tiles > 1:
            detail += f"  ·  {tiles} tiles"
        if score < SCORE_WORTH_NOTING:
            detail += f"  ·  {score:.0f}"
        under = ElidedLabel(detail)
        under.setStyleSheet(
            f"color: {theme.WARN if score < SCORE_WORTH_NOTING else theme.TEXT_DIM};")
        words.addWidget(under)
        holder = QWidget()
        holder.setLayout(words)
        holder.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
        row.addWidget(holder, 1)

    def keeping(self) -> bool:
        return self.keep.isChecked()

    def counted(self) -> int:
        return self.count.value()

    def edited(self) -> bool:
        """Whether the number was changed from what the scan counted."""
        return self.count.value() != self._scanned

    def scanned(self) -> int:
        return self._scanned

    def toggle(self) -> None:
        self.keep.setChecked(not self.keep.isChecked())
