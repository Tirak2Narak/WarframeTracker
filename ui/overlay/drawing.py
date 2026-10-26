"""Painting the palette grid. Pure drawing: no window, no process, no config file.

The grid is the one an earlier standalone script drew:
columns A to E from the left, rows 1 to 18 from the top, a margin around it
for the letters and numbers. The cell names are the ones the colour palette in
the Arsenal is talked about with ("B7"), so a cell can be named instead of
counted.

Everything the look depends on is in one style dict (`DEFAULT_STYLE`, changed
by `style_from_config`), so the helper process can be sent a new look as plain
JSON and the tests can draw without a window.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

#: Size of the grab handle in the aligning mode, bottom right.
HANDLE = 22

#: Colour of the dashed frame and the handle while aligning.
ALIGN_COLOUR = "#ffd400"

DEFAULT_STYLE = {
    "columns": 5,
    "rows": 18,
    #: Room around the grid for the letters and numbers, at least; it grows with
    #: the font (`margin_px`).
    "margin": 34,
    "font_pt": 15,
    "line_px": 1.5,
    "line_opacity": 0.55,
    "cell_names": False,
    "colours": {"line": "#ffffff", "text": "#ffffff", "outline": "#000000"},
}


def merge_style(raw: dict | None) -> dict:
    """`DEFAULT_STYLE` with the keys of `raw` laid over it.

    The one place that checks a style, whether it comes from the configuration
    or over the socket to the helper: a value of the wrong kind falls back to
    the default, a number is held inside its range, unknown keys are ignored.
    """
    style = {**DEFAULT_STYLE, "colours": dict(DEFAULT_STYLE["colours"])}
    raw = raw or {}
    for key, kind, low, high in (("columns", int, 1, 26), ("rows", int, 1, 99),
                                 ("margin", int, 0, 400), ("font_pt", int, 6, 60),
                                 ("line_px", float, 0.5, 10.0),
                                 ("line_opacity", float, 0.05, 1.0)):
        try:
            value = kind(raw[key])
        except (KeyError, TypeError, ValueError):
            continue
        style[key] = min(max(value, low), high)
    if "cell_names" in raw:
        style["cell_names"] = bool(raw["cell_names"])
    colours = raw.get("colours")
    if isinstance(colours, dict):
        for name in style["colours"]:
            if isinstance(colours.get(name), str) and QColor(colours[name]).isValid():
                style["colours"][name] = colours[name]
    return style


def style_from_config(cfg) -> dict:
    """The style the `[palette_grid]` section of the configuration asks for.

    Takes anything with `get(dotted, default)`.
    """
    keys = ("columns", "rows", "font_pt", "line_opacity", "cell_names")
    return merge_style({key: cfg.get(f"palette_grid.{key}") for key in keys})


def margin_px(style: dict) -> int:
    """The margin around the grid: the configured one, or more for a big font."""
    return max(int(style["margin"]), int(float(style["font_pt"]) * 2.3))


def column_name(index: int) -> str:
    return chr(ord("A") + index)


def cell_name(column: int, row: int) -> str:
    """The name of a cell: column letter and row number, "B7"."""
    return f"{column_name(column)}{row + 1}"


def draw_outlined_text(painter: QPainter, rect: QRectF, text: str, style: dict) -> None:
    """`text` centred in `rect`, with a dark outline so it reads on any colour."""
    colours = style["colours"]
    path = QPainterPath()
    metrics = painter.fontMetrics()
    x = rect.center().x() - metrics.horizontalAdvance(text) / 2
    y = rect.center().y() + (metrics.ascent() - metrics.descent()) / 2
    path.addText(QPointF(x, y), painter.font(), text)
    painter.setPen(QPen(QColor(colours["outline"]), 3.5, Qt.SolidLine,
                        Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
    painter.fillPath(path, QColor(colours["text"]))


def draw_grid(painter: QPainter, area: QRect, style: dict, aligning: bool) -> None:
    """Paint the grid with its letters and numbers into `area`.

    `area` is the whole window: the grid is `area` minus the margin on every
    side. While aligning, the window gets a faint dark veil, a dashed frame and
    a handle in the bottom right corner.
    """
    painter.setRenderHint(QPainter.Antialiasing)
    margin = margin_px(style)
    columns, rows = int(style["columns"]), int(style["rows"])
    colours = style["colours"]
    grid = QRectF(area.adjusted(margin, margin, -margin, -margin))
    cell_w, cell_h = grid.width() / columns, grid.height() / rows

    if aligning:
        whole = QRectF(area)
        painter.fillRect(whole, QColor(0, 0, 0, 70))
        painter.setPen(QPen(QColor(ALIGN_COLOUR), 2, Qt.DashLine))
        painter.drawRect(whole.adjusted(1, 1, -1, -1))
        painter.fillRect(QRectF(whole.width() - HANDLE, whole.height() - HANDLE,
                                HANDLE, HANDLE), QColor(ALIGN_COLOUR))

    opacity = float(style["line_opacity"])
    line = QColor(colours["line"])
    line.setAlphaF(opacity)
    dark = QColor(0, 0, 0, int(255 * opacity))
    width = float(style["line_px"])
    # The dark line first and wider, the light one on top: visible on light and
    # on dark palette colours alike.
    for colour, pen_width in ((dark, width + 2), (line, width)):
        painter.setPen(QPen(colour, pen_width))
        for i in range(columns + 1):
            x = grid.left() + i * cell_w
            painter.drawLine(QPointF(x, grid.top()), QPointF(x, grid.bottom()))
        for j in range(rows + 1):
            y = grid.top() + j * cell_h
            painter.drawLine(QPointF(grid.left(), y), QPointF(grid.right(), y))

    font = QFont()
    font.setPointSizeF(float(style["font_pt"]))
    font.setBold(True)
    painter.setFont(font)
    for i in range(columns):
        for top in (grid.top() - margin, grid.bottom()):
            draw_outlined_text(painter, QRectF(grid.left() + i * cell_w, top, cell_w, margin),
                               column_name(i), style)
    for j in range(rows):
        for left in (grid.left() - margin, grid.right()):
            draw_outlined_text(painter, QRectF(left, grid.top() + j * cell_h, margin, cell_h),
                               str(j + 1), style)
    if style["cell_names"]:
        font.setPointSizeF(float(style["font_pt"]) * 0.7)
        painter.setFont(font)
        for i in range(columns):
            for j in range(rows):
                draw_outlined_text(
                    painter, QRectF(grid.left() + i * cell_w, grid.top() + j * cell_h,
                                    cell_w, cell_h * 0.45), cell_name(i, j), style)


# -- the reward overlay --------------------------------------------------
#
# Everything is a fraction of the game window's height (sizes) or of its width
# and height (places), as the regions are, so one drawing fits 1080p and 4K.
# The places were measured on 379 reward pictures at 4K:
# cards y 0.206 to 0.425; the countdown emblem x 0.425 to 0.575, its number
# y 0.115 to 0.173, its lower decoration to y 0.194, its decoration left of the
# number from x 0.425; the first row of player names at y 0.475, a second at
# 0.502 (with "+N"), a third at 0.531, ending at 0.541 at the lowest of 227
# blue-theme pictures; a fourth row: see NAMES_END below.

#: The look: the frame's colour and the colour of what the tool says,
#: as "#rrggbb". The tracker works out the two from `[overlay] colours`
#: (`ui.overlay.looks`); the helper only takes them over, anything that is not a
#: colour falls back to this red and green.
DEFAULT_LOOK = {"frame": "#ff2a2a", "text": "#3ddc6b"}

FRAME_COLOUR = DEFAULT_LOOK["frame"]
TEXT_COLOUR = DEFAULT_LOOK["text"]
NOTE_COLOUR = "#ffc83d"
OUTLINE_COLOUR = "#000000"

#: Where the box with the info lines starts: just under the lowest end of the
#: fourth row of player names (all 4 players take one card; with the "+N" and its
#: icon it ends at 0.575 at the lowest of the 338 reward screens), so the box
#: may lie over the game's Steel Path box.
NAMES_END = 0.575
NAMES_GAP = 0.002
#: Distance between two info lines, and their font size (pixels).
INFO_STEP = 0.019
INFO_FONT = 0.0155
#: The padding of the dark box behind the lines, as a share of the font, and its look.
BOX_PAD_X = 0.5
BOX_PAD_Y = 0.32
BOX_RGBA = (8, 10, 16, 204)
#: Size of "TAKE THIS" and of the text that stands in for it.
TAKE_FONT = 0.022
#: Gap between the bottom of "TAKE THIS" and the top of the card's frame.
TAKE_GAP = 0.008
#: Where the text for "nothing to take" stands: centred on NOTE_X, baseline at
#: NOTE_Y, in the free space left of the countdown above the first card.
NOTE_X = 0.33
NOTE_Y = 0.18
FRAME_WIDTH = 0.0035
#: A thin dark line round the frame's outside, so an orange or red frame can be
#: seen on a light picture; as a share of the frame's width.
FRAME_RIM = 0.3
#: The countdown's number, which nothing may cover, for the tests.
EMBLEM_CLEAR = (0.44, 0.110, 0.12, 0.062)


def merge_look(raw: dict | None) -> dict:
    """`DEFAULT_LOOK` with the valid colours of `raw` laid over it (the tracker's
    command): a value that is not a colour falls back to the default. Returns
    normalised "#rrggbb"."""
    look = dict(DEFAULT_LOOK)
    for key in look:
        value = (raw or {}).get(key)
        if isinstance(value, str) and value.startswith("#"):
            colour = QColor(value)
            if colour.isValid():
                look[key] = colour.name()
    return look


def _font(pixels: float) -> QFont:
    font = QFont()
    font.setPixelSize(max(6, int(round(pixels))))
    font.setBold(True)
    return font


def draw_text_at(painter: QPainter, centre_x: float, baseline: float, text: str,
                 pixels: float, fill: str, max_width: float | None = None) -> None:
    """`text` centred on `centre_x` with its baseline at `baseline`, filled and
    with a dark outline; shrunk to `max_width` when it would be wider."""
    font = _font(pixels)
    metrics = QFontMetricsF(font)
    width = metrics.horizontalAdvance(text)
    if max_width and width > max_width:
        font = _font(pixels * max_width / width)
        metrics = QFontMetricsF(font)
        width = metrics.horizontalAdvance(text)
    path = QPainterPath()
    path.addText(QPointF(centre_x - width / 2, baseline), font, text)
    outline = max(2.0, pixels * 0.16)
    painter.setPen(QPen(QColor(OUTLINE_COLOUR), outline, Qt.SolidLine,
                        Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
    painter.fillPath(path, QColor(fill))


def draw_check(painter: QPainter, centre: QPointF, radius: float, fill: str = TEXT_COLOUR) -> None:
    """A disc with a white tick: "you took this one"."""
    painter.setPen(QPen(QColor(OUTLINE_COLOUR), max(2.0, radius * 0.18)))
    painter.setBrush(QColor(fill))
    painter.drawEllipse(centre, radius, radius)
    tick = QPainterPath()
    tick.moveTo(centre.x() - radius * 0.5, centre.y() + radius * 0.02)
    tick.lineTo(centre.x() - radius * 0.12, centre.y() + radius * 0.42)
    tick.lineTo(centre.x() + radius * 0.52, centre.y() - radius * 0.38)
    painter.setPen(QPen(QColor("#ffffff"), max(2.5, radius * 0.26), Qt.SolidLine,
                        Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(tick)


def frame_line(height: float) -> float:
    """The frame's pen width in pixels for a window `height` pixels high."""
    return max(2.0, FRAME_WIDTH * height)


def frame_rect(rect: QRectF, height: float) -> QRectF:
    """The path the frame's pen follows. The pen is centred on it, so it lies half
    a line outside the card's edge: the frame's inner edge is the card's edge and
    nothing of the card is covered."""
    half = frame_line(height) / 2
    return rect.adjusted(-half, -half, half, half)


def info_blocks(width: float, height: float, tiles: list, lines: list) -> list:
    """Where the info lines of every card stand, in pixels of a window `width` by
    `height` (origin at the window's top left).

    One entry per card: None without lines, else a dict with `box` (the dark box,
    a QRectF), `lines` (the lines shown, shrunk to the card), `pixels` (their font
    size), `baselines` (one y per line), `centre` (x) and `max_width`. Drawing and
    tests use this one function.

    The box top is just under the lowest the fourth row of player names reaches;
    every card gets all its lines at the full size. The box is not wider
    than its card and may cover the game's Steel Path box.
    """
    out: list = []
    box_top = (NAMES_END + NAMES_GAP) * height
    for index, (tx, _ty, tw, _th) in enumerate(tiles):
        card_lines = [str(line) for line in (lines[index] if index < len(lines) else [])[:3] if line]
        if not card_lines:
            out.append(None)
            continue
        card_w = tw * width
        centre = (tx + tw / 2) * width
        pixels = INFO_FONT * height
        metrics = QFontMetricsF(_font(pixels))
        text_w = max(min(metrics.horizontalAdvance(t), card_w * 0.98 - 2 * BOX_PAD_X * pixels)
                     for t in card_lines)
        box_w = min(text_w + 2 * BOX_PAD_X * pixels, card_w)
        step = INFO_STEP * height
        box_h = (2 * BOX_PAD_Y * pixels + (len(card_lines) - 1) * step
                 + metrics.ascent() + metrics.descent())
        box = QRectF(centre - box_w / 2, box_top, box_w, box_h)
        first = box.top() + BOX_PAD_Y * pixels + metrics.ascent()
        out.append({"box": box, "lines": card_lines, "pixels": pixels, "centre": centre,
                    "baselines": [first + n * step for n in range(len(card_lines))],
                    "max_width": box.width() - 2 * BOX_PAD_X * pixels})
    return out


def draw_rewards(painter: QPainter, area: QRect, tiles: list, best: int | None,
                 headline: str | None, lines: list, picked: int | None = None,
                 look: dict | None = None) -> None:
    """Paint the reward overlay into `area`, which is the game window.

    `tiles` are the cards as fractions of the window (x, y, w, h), left to right;
    `best` the index of the recommended card or None; `headline` the words for
    "nothing to take" (shown only when `best` is None); `lines` the info lines
    under each card; `picked` the card the player chose, which gets a tick;
    `look` the colours (`merge_look`).
    Nothing is drawn over the cards' middles or the countdown: the frame lies
    outside the card's edge, "TAKE THIS" stands in the gap above the card, the
    info lines in a dark box under the player names.
    """
    painter.setRenderHint(QPainter.Antialiasing)
    look = merge_look(look)
    width, height = float(area.width()), float(area.height())
    left, top = float(area.x()), float(area.y())
    blocks = info_blocks(width, height, tiles, lines)

    for index, (tx, ty, tw, th) in enumerate(tiles):
        rect = QRectF(left + tx * width, top + ty * height, tw * width, th * height)
        centre = rect.center().x()
        block = blocks[index]
        if block is not None:
            box = block["box"].translated(left, top)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(*BOX_RGBA))
            painter.drawRoundedRect(box, block["pixels"] * 0.45, block["pixels"] * 0.45)
            for baseline, text in zip(block["baselines"], block["lines"]):
                draw_text_at(painter, block["centre"] + left, top + baseline, text,
                             block["pixels"], look["text"], block["max_width"])
        if index == best:
            line = frame_line(height)
            path = frame_rect(rect, height)
            rim = max(1.0, line * FRAME_RIM)
            painter.setBrush(Qt.NoBrush)
            outer = path.adjusted(-(line + rim) / 2, -(line + rim) / 2, (line + rim) / 2, (line + rim) / 2)
            painter.setPen(QPen(QColor(OUTLINE_COLOUR), rim))
            painter.drawRect(outer)
            painter.setPen(QPen(QColor(look["frame"]), line))
            painter.drawRect(path)
            draw_text_at(painter, centre, rect.top() - line - TAKE_GAP * height,
                         "TAKE THIS", TAKE_FONT * height, look["text"], rect.width() * 1.1)
        if picked is not None and index == picked:
            radius = 0.021 * height
            draw_check(painter, QPointF(rect.right() - radius * 1.5,
                                        rect.top() + radius * 1.5), radius, look["text"])
    if best is None and headline and tiles:
        draw_text_at(painter, left + width * NOTE_X, top + NOTE_Y * height, headline,
                     TAKE_FONT * height, NOTE_COLOUR, width * 0.15)
