"""Colours, fonts and the application stylesheet.

Sized in points, not pixels, so the interface scales correctly on the 4K
monitor of a multi-monitor setup without Qt's per-screen HiDPI scaling
fighting against hardcoded pixel sizes. Qt6 enables high-DPI scaling by
default; this module only has to avoid undoing that.

Themes are token sets, the way two earlier attempts did it - one as CSS
custom properties, the other as a dict per theme - and the stylesheet is
generated from whichever set is active. The palettes from both are carried over
rather than reinvented, since they were already tuned against this game's own
screens, and joined by one taken from the game itself.

The recurring mark is a diamond. It is how Warframe flags a void reward, it is
already the shape of the application icon, and here it marks the selected page
and the head of each section - so the same symbol means "this one" throughout.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF

#: Every theme defines the same keys. A missing one is a bug rather than a
#: fallback: a half-painted interface is harder to spot than a wrong colour.
_KEYS = ("bg", "bg_elevated", "bg_card", "border", "text", "text_dim",
         "text_faint", "accent", "accent_dim", "good", "warn", "bad",
         "vaulted", "junk")

THEMES: dict[str, dict[str, str]] = {
    # Taken from the game rather than from a dashboard template. Warframe draws
    # its reward screens in Orokin gold on void black, and gold is what it uses
    # for the things this tool is about: relics, rewards, ducats.
    "Orokin": {
        "bg": "#0c0f14", "bg_elevated": "#141922", "bg_card": "#1a2029",
        "border": "#262e3a", "text": "#e6e8ec", "text_dim": "#939aa8",
        "text_faint": "#7f889a", "accent": "#d9a441", "accent_dim": "#6b5220",
        "good": "#58b98a", "warn": "#d9a441", "bad": "#d96b6b",
        "vaulted": "#a97fe0", "junk": "#7f889a",
    },
    # The previous attempt's own palette, carried over intact: indigo panels
    # with a red accent, which is the closest of these to the game's UI chrome.
    "Midnight": {
        "bg": "#1a1a2e", "bg_elevated": "#16213e", "bg_card": "#1e1e3f",
        "border": "#2a2a4e", "text": "#eaeaea", "text_dim": "#a0a0a0",
        "text_faint": "#8a8a8a", "accent": "#eb546c", "accent_dim": "#7a2435",
        "good": "#4caf50", "warn": "#ff9800", "bad": "#f54e42",
        "vaulted": "#b98af2", "junk": "#8a8a8a",
    },
    # From an earlier attempt, unchanged.
    "Blue Steel": {
        "bg": "#1a202c", "bg_elevated": "#2d3748", "bg_card": "#2d3748",
        "border": "#4a5568", "text": "#ffffff", "text_dim": "#a6b3c4",
        "text_faint": "#95a1b1", "accent": "#5ba6e5", "accent_dim": "#2b5f8a",
        "good": "#48bb78", "warn": "#ed8936", "bad": "#ee7f7f",
        "vaulted": "#b794f4", "junk": "#95a1b1",
    },
    "Dark": {
        "bg": "#0f1419", "bg_elevated": "#1e2328", "bg_card": "#2a3138",
        "border": "#30363d", "text": "#c9d1d9", "text_dim": "#a2aab2",
        "text_faint": "#9099a5", "accent": "#58a6ff", "accent_dim": "#1f4b7a",
        "good": "#3fb950", "warn": "#d29922", "bad": "#f96b65",
        "vaulted": "#bc8cff", "junk": "#9099a5",
    },
    # Included because an earlier attempt had it. The Rewards page follows the
    # theme like every other page.
    "Light": {
        "bg": "#f7fafc", "bg_elevated": "#ffffff", "bg_card": "#edf2f7",
        "border": "#e2e8f0", "text": "#1a202c", "text_dim": "#4a5568",
        "text_faint": "#5b6f89", "accent": "#2a70b2", "accent_dim": "#bee3f8",
        "good": "#2b7b50", "warn": "#8d671c", "bad": "#d71d1d",
        "vaulted": "#7b53d3", "junk": "#5b6f89",
    },
}

DEFAULT_THEME = "Orokin"

#: Condensed for headings, because the game sets its own in narrow capitals and
#: because a condensed face holds a long item name in a table column without
#: truncating it. Body text stays a normal width - condensed body copy is harder
#: to read and buys nothing here.
DISPLAY_FAMILY = "Fira Sans Condensed, DejaVu Sans Condensed, sans-serif"
FONT_FAMILY = "Noto Sans, DejaVu Sans, sans-serif"
MONO_FAMILY = "Noto Sans Mono, DejaVu Sans Mono, monospace"

#: Base point size, adjustable in Settings. An earlier attempt had the same
#: control, and it earns its place on a desk where one screen is 4K and the
#: others are not.
#: Base point size, adjustable in Settings. The ceiling is deliberately far
#: above what looks sensible in a mockup: on a 4K panel without desktop scaling
#: a point is a quarter the size it is at 1080p, and 16pt there is still small
#: enough to squint at. Someone who does not need 28 will never reach it.
DEFAULT_FONT_PT = 11
MIN_FONT_PT = 8
MAX_FONT_PT = 28

# Module-level names for the active theme. Views import these directly
# (theme.GOOD, theme.ACCENT), so switching a theme rebinds them.
BG = BG_ELEVATED = BG_CARD = BORDER = ""
TEXT = TEXT_DIM = TEXT_FAINT = ACCENT = ACCENT_DIM = ON_ACCENT = ""
GOOD = WARN = BAD = VAULTED = JUNK = ACCENT_HOVER = CARD_HOVER = ""

_active = DEFAULT_THEME
_font_pt = DEFAULT_FONT_PT
_mark = 16
_mark_half = 10
_mark_ring = 5
_slider_handle = 16
_mark_row = 20
_slider_groove = 6
_edge_hair = 1
_edge = 3
_edge_strong = 6
_r_control = 6
_r_card = 10
_row_pad = 7
_cell_pad = 9
_gap = 9
_scroll = 12
_arrow_box = 26
_arrow_w = 10
_arrow_h = 6
_spin_w = 22
_spin_arrow = 8


_glyph_dir: str | None = None


def cache_dir() -> str:
    """The tracker's folder in the user's cache; only the glyphs below live there."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "warframe-tracker")


def _glyph(shape: str, colour: str, width: int, height: int | None = None) -> str:
    """The path of a small PNG a stylesheet can name in `image: url(...)`.

    A Qt stylesheet takes a file or a resource, not a data URI (measured:
    `url(data:...)` draws nothing), and the arrow of a drop-down, the buttons of
    a spin box and the tick of a check box are images in it. They are drawn
    here in the theme's own colours at the size the text needs, so there is no
    image file in the repository to fall out of step with the palette.

    Into the user's cache, named by shape, colour and size, and reused by every
    later run. A folder per run removed at exit was tried first and left 98
    behind in /tmp within an afternoon: `atexit` does not run when a process is
    ended by a signal, which is how a desktop session ends one. Returns "" when
    nothing can be written; the rule then says `image: none` and the box is
    plain.
    """
    global _glyph_dir
    height = height or width
    try:
        if _glyph_dir is None:
            folder = os.path.join(cache_dir(), "glyphs")
            os.makedirs(folder, exist_ok=True)
            _glyph_dir = folder
        path = os.path.join(
            _glyph_dir, f"{shape}-{colour.lstrip('#')}-{width}x{height}.png")
        if not os.path.exists(path):
            image = QImage(width, height, QImage.Format_ARGB32)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing)
            ink = QColor(colour)
            if shape == "check":
                painter.setPen(QPen(ink, max(2.0, width * 0.15), Qt.SolidLine,
                                    Qt.RoundCap, Qt.RoundJoin))
                painter.drawPolyline(QPolygonF([
                    QPointF(width * 0.24, height * 0.54),
                    QPointF(width * 0.43, height * 0.72),
                    QPointF(width * 0.77, height * 0.30)]))
            else:
                # A triangle on the horizontal middle, pointing down or up,
                # one pixel in from every edge so the point is not clipped.
                top, bottom = (height - 1.0, 1.0) if shape == "up" \
                    else (1.0, height - 1.0)
                painter.setPen(Qt.NoPen)
                painter.setBrush(ink)
                painter.drawPolygon(QPolygonF([
                    QPointF(1.0, top), QPointF(width - 1.0, top),
                    QPointF(width / 2.0, bottom)]))
            painter.end()
            # Saved under another name and moved into place, so a second
            # instance starting at the same moment never reads half a file.
            partial = f"{path}.{os.getpid()}.part"
            if not image.save(partial, "PNG"):
                return ""
            os.replace(partial, path)
        return path
    except OSError:
        return ""


def _image(shape: str, colour: str, width: int, height: int | None = None) -> str:
    path = _glyph(shape, colour, width, height)
    return f"url({path})" if path else "none"


def names() -> list[str]:
    return list(THEMES)


def active() -> str:
    return _active


def font_size() -> int:
    return _font_pt


def readable_on(hex_colour: str) -> str:
    """Black or white, whichever can be read on that colour.

    Computed rather than listed per theme, because getting it wrong is not a
    matter of taste: Orokin's accent is a light gold, and the white text that
    suited every other theme's accent was unreadable on it. Whichever of the
    two has the higher WCAG contrast ratio against the colour. It was a cut on
    the colour's own luminance, which left white on Midnight's red at 3.5 and
    white on Blue Steel's blue at 3.1 while the dark text had 5 and 6.8.
    """
    def luminance(value: str) -> float:
        value = value.lstrip("#")
        channels = []
        for i in (0, 2, 4):
            part = int(value[i:i + 2], 16) / 255
            channels.append(part / 12.92 if part <= 0.03928
                            else ((part + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def contrast(first: str, second: str) -> float:
        light, dark = sorted((luminance(first), luminance(second)), reverse=True)
        return (light + 0.05) / (dark + 0.05)

    return max(("#14181f", "#ffffff"),
               key=lambda ink: contrast(ink, hex_colour))


# -- the scale ------------------------------------------------------------
#
# Ninety-odd colours and sixteen different font sizes were written directly
# into the views, which had two consequences worth naming. Switching to the
# Light theme left "4/4" in a pale green nobody could read on white, because
# those greens were not the theme's. And "0.864em", "0.955em", "1.136em" and
# "1.182em" all existed side by side, which is not a scale, it is four people
# guessing.
#
# Everything below is relative to the base font, because that is a setting the
# user actually changes - 11pt by default and 20pt on a 4K panel. A design
# fixed in pixels stops being a design the moment that slider moves.

#: Type scale, as multiples of the base size. Six steps, each far enough from
#: the next to be told apart at a glance: anything closer is a difference the
#: reader has to measure rather than see.
TYPE = {
    "display": 2.20,   # the one number or name a screen is about
    "title":   1.45,   # page titles
    "section": 1.10,   # card and section headings
    "body":    1.00,
    "small":   0.88,   # captions, secondary lines
    "micro":   0.78,   # provenance, timestamps, build strings
}

#: Spacing, in multiples of the line height. Four steps and no more: a scale
#: with an answer for every gap is a scale nobody follows.
SPACE = {"tight": 0.25, "close": 0.5, "step": 0.9, "gap": 1.5, "apart": 2.4}

#: Corner radii, in pixels at the base size. Rounded enough to read as panels,
#: not so round they read as pills.
RADIUS = {"small": 6, "medium": 10, "large": 14}

#: How thick the rank edge is. The one motif this interface repeats: anything
#: ranked carries a coloured bar on its leading edge, and the colour says how
#: much it matters. Same rule on a reward card, a plan row and a review row.
EDGE = 4


def space(name: str) -> int:
    """A gap from the scale, in pixels at the current text size."""
    return max(2, round(_font_pt * 96 / 72 * SPACE.get(name, 1.0)))


def size(name: str) -> float:
    """A font size from the scale, as an em multiple."""
    return TYPE.get(name, 1.0)


def text(role: str = "body", colour: str | None = None,
         weight: int | None = None) -> str:
    """A stylesheet fragment for one piece of text.

    Written as `label.setStyleSheet(theme.text("small", theme.TEXT_DIM))`
    rather than as a literal, so a theme change reaches it and so the sizes
    stay on the scale. The colour defaults to the theme's body colour, which
    is the whole point: a hardcoded grey is invisible on a light background
    and nobody notices until they switch.
    """
    parts = [f"font-size: {TYPE.get(role, 1.0):.3f}em",
             f"color: {colour or TEXT}"]
    if weight:
        parts.append(f"font-weight: {weight}")
    if role in ("display", "title", "section"):
        parts.append(f"font-family: {DISPLAY_FAMILY}")
    return "; ".join(parts) + ";"



def section_text(colour: str | None = None) -> str:
    """The heading of a card that has a diamond: Section on every page.

    One function for it because the card heading was written out by hand in
    four places in four sizes (1.182em, 1.364em, 1.273em, 1.091em) - the scale
    has one step for it, `section` (audit C3).
    """
    return text("section", colour, 600) + " letter-spacing: 0.5px;"


def label_indent() -> int:
    """Where the text of a radio button or box starts, in pixels.

    The indicator including its border, plus the gap the stylesheet puts after
    it. A note meant to stand under the text takes this as its left padding:
    22px was 3px off at 11pt and 10px off at 20pt (audit C1).
    """
    return _mark + 4 + _gap


def px(em: float) -> int:
    """One em in pixels, at the 96dpi Qt assumes.

    Needed because Qt's own API takes integers - `setContentsMargins`,
    `setMinimumHeight` - where the stylesheet takes em. Written as pixels those
    calls said one thing at 11pt and half as much, proportionally, at the 20pt
    a player actually runs: a card with 28 pixels of margin around 20pt text is
    tighter than the same card around 11pt text, and the page reads as cramped
    without anything being obviously wrong.
    """
    return max(1, round(_font_pt * 96 / 72 * em))


#: The margins a page keeps from the window edge, and a card from its own
#: contents. Named rather than numbered at the call site: "page" and "card"
#: survive a change of mind about the value, "28, 24, 28, 16" does not.
def page_margins() -> tuple[int, int, int, int]:
    return px(1.6), px(1.3), px(1.6), px(1.0)


def card_margins() -> tuple[int, int, int, int]:
    return px(1.1), px(0.95), px(1.1), px(0.95)


def apply(name: str = DEFAULT_THEME, font_pt: int = DEFAULT_FONT_PT) -> str:
    """Make a theme active and return its stylesheet.

    Rebinding module globals is unusual, and the alternative was worse: every
    view already reads `theme.GOOD` and `theme.ACCENT` at paint time, so a
    theme object threaded through every constructor would touch every file to
    say the same thing. The names are read-only to everything outside here.
    """
    global _active, _font_pt
    global BG, BG_ELEVATED, BG_CARD, BORDER, TEXT, TEXT_DIM, TEXT_FAINT
    global ACCENT, ACCENT_DIM, ON_ACCENT, GOOD, WARN, BAD, VAULTED, JUNK

    palette = THEMES.get(name)
    if palette is None:
        name, palette = DEFAULT_THEME, THEMES[DEFAULT_THEME]
    missing = [key for key in _KEYS if key not in palette]
    if missing:
        raise KeyError(f"theme {name!r} is missing {', '.join(missing)}")

    _active = name
    _font_pt = max(MIN_FONT_PT, min(MAX_FONT_PT, int(font_pt)))
    # Points to pixels at the 96dpi Qt assumes for stylesheets. Needed because
    # the indicator sizes below cannot be written in em.
    global _mark, _mark_half, _mark_ring
    _mark = max(12, round(_font_pt * 96 / 72 * 0.85))
    _mark_half = _mark // 2 + 2
    _mark_ring = max(3, _mark // 3)
    # The text-size slider was a 14px knob on a 6px rail at every size: at
    # 17pt on a 4K screen a target smaller than the letters beside it.
    global _slider_handle, _slider_groove, _mark_row
    # A box in a table row is the target for a row two lines tall; at the
    # size of a box beside a label it read as a speck.
    _mark_row = max(18, round(_font_pt * 96 / 72 * 1.25))
    _slider_handle = max(16, round(_font_pt * 96 / 72 * 1.0))
    _slider_groove = max(6, round(_font_pt * 96 / 72 * 0.3))
    # The judgement edge, in three weights. The one motif this interface
    # repeats: anything nameable is introduced from the left by a coloured
    # edge, and the colour is a judgement rather than decoration.
    global _edge_hair, _edge, _edge_strong, _r_control, _r_card
    _edge_hair = 1
    _edge = max(2, round(_font_pt * 96 / 72 * 0.15))
    _edge_strong = max(3, round(_font_pt * 96 / 72 * 0.30))
    _r_control = min(8, max(4, round(_font_pt * 96 / 72 * 0.30)))
    # Rows breathe instead of being ruled apart. Measured against the text so
    # a 20pt list is not a 11pt list with bigger letters crammed into it.
    global _row_pad, _cell_pad
    _row_pad = max(4, round(_font_pt * 96 / 72 * 0.42))
    _cell_pad = max(6, round(_font_pt * 96 / 72 * 0.55))
    _r_card = min(16, max(8, round(_font_pt * 96 / 72 * 0.60)))
    # Parts of the controls that were fixed pixels at every text size. Each is
    # a multiple of the font's pixel size, like the indicators above.
    global _gap, _scroll, _arrow_box, _arrow_w, _arrow_h, _spin_w, _spin_arrow
    font_px = _font_pt * 96 / 72
    _gap = max(8, round(font_px * 0.62))
    _scroll = max(12, round(font_px * 0.8))
    _arrow_box = max(22, round(font_px * 1.7))
    _arrow_w = max(10, round(font_px * 0.7))
    _arrow_h = max(5, round(_arrow_w * 0.55))
    _spin_w = max(22, round(font_px * 1.5))
    _spin_arrow = max(9, round(font_px * 0.55))

    BG, BG_ELEVATED = palette["bg"], palette["bg_elevated"]
    BG_CARD, BORDER = palette["bg_card"], palette["border"]
    TEXT, TEXT_DIM = palette["text"], palette["text_dim"]
    TEXT_FAINT = palette["text_faint"]
    ACCENT, ACCENT_DIM = palette["accent"], palette["accent_dim"]
    ON_ACCENT = readable_on(ACCENT)
    # Derived, not written down. A literal blue here turned the primary button
    # blue on hover under the gold Orokin theme - the one hex that survived the
    # sweep, in the file whose whole job is not to have any.
    global ACCENT_HOVER
    ACCENT_HOVER = QColor(ACCENT).lighter(118).name()
    global CARD_HOVER
    CARD_HOVER = QColor(BG_CARD).lighter(118).name()
    GOOD, WARN, BAD = palette["good"], palette["warn"], palette["bad"]
    VAULTED, JUNK = palette["vaulted"], palette["junk"]
    return stylesheet()



def judgement_for(colour: str) -> str:
    """Which judgement a theme colour stands for, for the left edge.

    Callers already pass a colour - `Section(title, note, theme.GOOD)` - so the
    edge asks what that colour meant rather than making every call site say it
    twice. An unknown colour gets the neutral edge, which is the honest answer
    for a colour that is not one of the roles.
    """
    return {GOOD: "good", WARN: "warn", BAD: "bad", VAULTED: "vaulted",
            ACCENT: "accent", JUNK: "quiet", TEXT_FAINT: "quiet"}.get(colour, "")


def mark(widget, judgement: str) -> None:
    """Give a widget the left edge for a judgement, and make Qt notice.

    A dynamic property set after the stylesheet is applied changes nothing
    until the style is asked to look again; without the unpolish/polish pair
    the edge appears on the next theme change and not before.
    """
    widget.setProperty("judgement", judgement or "")
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def base_font(point_size: int | None = None) -> QFont:
    font = QFont(FONT_FAMILY.split(",")[0].strip())
    font.setPointSize(point_size if point_size is not None else _font_pt)
    return font


def qcolor(hex_value: str) -> QColor:
    return QColor(hex_value)


def stylesheet() -> str:
    """The sheet for whichever theme is active."""
    return f"""
* {{
    font-family: {FONT_FAMILY};
}}

QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: {FONT_FAMILY};
    font-size: {_font_pt}pt;
}}

QMainWindow {{
    background-color: {BG};
}}

/* Text does not paint a background. The rule above sets one on every QWidget,
   and a QLabel is a QWidget - so every piece of text in the application was
   painting the page colour behind itself. Inside a card, whose colour is
   lighter, that showed as a dark rectangle around each line: the stats strip
   read as four ragged boxes rather than four figures, and every card in the
   application had patches in it. Transparent is what a label wants; anything
   that genuinely needs a background asks for one by name below. */
QLabel {{
    background: transparent;
}}

/* Qt's tooltip is a QLabel subclass, and a stylesheet selector matches
   subclasses - so the rule above takes its background away too, leaving it to
   render against whatever is behind it. Given one of its own: there are
   sixteen tooltips in this interface and every one of them was about to become
   unreadable. */
QToolTip {{
    background-color: {BG_ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 6px;
}}

#Sidebar {{
    background-color: {BG_ELEVATED};
    border-right: 1px solid {BORDER};
}}

QLabel#StatNumber {{
    font-family: {DISPLAY_FAMILY};
}}

#SidebarTitle {{
    font-family: {DISPLAY_FAMILY};
    color: {TEXT};
    font-size: 1.450em;
    font-weight: 600;
    padding: 1.20em 1.07em 0.27em 1.07em;
}}

#SidebarSubtitle {{
    color: {TEXT_FAINT};
    font-size: 0.780em;
    padding: 0px 0.30em 0.60em 1.07em;
}}

QListWidget#NavList {{
    background: transparent;
    border: none;
    outline: none;
    font-family: {DISPLAY_FAMILY};
    font-size: 1.100em;
    letter-spacing: 0.5px;
    padding: 0.27em 0.53em;
}}

/* A group heading is not selectable and does not carry the edge - it is a
   label that happens to live in a list, because a QListWidget has no other
   kind of row. */
QListWidget#NavList::item:disabled {{
    color: {TEXT_FAINT};
    font-size: 0.780em;
    letter-spacing: 2px;
    padding: 1.10em 0.93em 0.35em 0.93em;
    border-left: none;
}}

/* Ten pages and three headings must fit a 1150 px window at 20pt without a
   scroll bar (the smoke test). A row measured 71px at 0.52em, 59px at 0.35em
   and 55px at 0.30em, and the 13 rows have 758px: the Trading group took this
   from 0.52em (8 pages, 2 headings) to 0.32em. */
QListWidget#NavList::item {{
    padding: 0.32em 0.93em;
    border-left: {_edge_strong}px solid transparent;
    border-radius: 0px;
    margin: 0px;
    color: {TEXT_DIM};
}}

/* The selected page is marked, not filled. The comment said so before the rule
   did: it set a block of colour behind the label, which competes with the page
   it introduces. An edge on the leading side states which one is current and
   leaves the eye to move on - and it is the same edge every card, section and
   reward carries, which is what holds these pages together as one thing. */
QListWidget#NavList::item:selected {{
    background: transparent;
    border-left: {_edge_strong}px solid {ACCENT};
    color: {ACCENT};
}}

QListWidget#NavList::item:hover:!selected {{
    border-left: {_edge_strong}px solid {BORDER};
    color: {TEXT};
}}

QSlider::groove:horizontal {{
    background: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: {_slider_groove // 2}px;
    height: {_slider_groove}px;
}}

QSlider::sub-page:horizontal {{
    background: {ACCENT_DIM};
    border-radius: {_slider_groove // 2}px;
}}

QSlider::handle:horizontal {{
    background: {ACCENT};
    border-radius: {_slider_handle // 2}px;
    width: {_slider_handle}px;
    margin: -{(_slider_handle - _slider_groove) // 2}px 0;
}}

QWidget#ReviewRow {{
    border-bottom: 1px solid {BORDER};
}}

QStatusBar {{
    background-color: {BG_ELEVATED};
    border-top: 1px solid {BORDER};
    color: {TEXT_DIM};
    font-size: 0.780em;
}}

QLabel#ViewTitle {{
    font-family: {DISPLAY_FAMILY};
    font-size: 2.000em;
    font-weight: 600;
    letter-spacing: 0.5px;
    color: {TEXT};
}}

QLabel#ViewSubtitle {{
    font-size: 1.000em;
    color: {TEXT_DIM};
}}

/* The two kinds of heading that are not a Section (audit C3). A dialog names
   what it shows in the title step, the card headings inside a page in the
   section step, so a page and a dialog read the same way. A Section's own
   heading is the same step, in the colour of its judgement. */
QLabel#DialogTitle {{
    font-family: {DISPLAY_FAMILY};
    font-size: {TYPE["title"]:.3f}em;
    font-weight: 600;
    color: {ACCENT};
}}

QLabel#CardHeading {{
    font-family: {DISPLAY_FAMILY};
    font-size: {TYPE["section"]:.3f}em;
    font-weight: 600;
    color: {TEXT};
}}

QLabel#SectionHeader {{
    font-family: {DISPLAY_FAMILY};
    font-size: 1.000em;
    font-weight: 600;
    color: {TEXT_DIM};
    text-transform: uppercase;
    letter-spacing: 1.5px;
}}

QFrame#StatsStrip {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QLabel#StatNumber {{
    font-size: 1.450em;
    font-weight: 600;
    color: {TEXT};
}}

QLabel#StatCaption {{
    font-size: 0.780em;
    color: {TEXT_DIM};
    text-transform: uppercase;
    letter-spacing: 1px;
}}

QLabel#StatBreakdown {{
    font-size: 0.880em;
    color: {TEXT_DIM};
}}

/* The one card that always carries a judgement: everything in it is finished.
   The full green outline shouted where the edge states it. */
QFrame#ReadyBox {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-left: {_edge_strong}px solid {GOOD};
    border-top-left-radius: 0px;
    border-bottom-left-radius: 0px;
    border-top-right-radius: {_r_card}px;
    border-bottom-right-radius: {_r_card}px;
}}

QLabel#ReadyHeader {{
    font-size: 1.000em;
    font-weight: 600;
    color: {GOOD};
}}

QLabel#ReadyBody {{
    font-size: 0.880em;
    color: {TEXT};
}}

/* Every card is introduced from the left. Neutral by default, and coloured
   when the card carries a judgement - set with
   `card.setProperty("judgement", "good")` and a repolish. Same rule as the
   navigation, the sections and the reward cards, which is the whole point:
   one edge, one meaning, every page. */
QFrame#Card {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-left: {_edge}px solid {BORDER};
    border-top-left-radius: 0px;
    border-bottom-left-radius: 0px;
    border-top-right-radius: {_r_card}px;
    border-bottom-right-radius: {_r_card}px;
}}

QFrame#Card[judgement="good"] {{ border-left-color: {GOOD}; }}
QFrame#Card[judgement="warn"] {{ border-left-color: {WARN}; }}
QFrame#Card[judgement="bad"] {{ border-left-color: {BAD}; }}
QFrame#Card[judgement="vaulted"] {{ border-left-color: {VAULTED}; }}
QFrame#Card[judgement="accent"] {{ border-left-color: {ACCENT}; }}
QFrame#Card[judgement="quiet"] {{ border-left-color: {JUNK}; }}

/* A QLabel paints its own background from the palette, which is the window
   colour, not the card's - so text inside a card sat in a darker box of its
   own. Tables likewise came with their own border and fill, which read as a
   panel inside a panel. Inside a card both are transparent and the card is the
   only surface. */
QLabel#StepNumber {{
    color: {ACCENT};
    font-size: 1.100em;
    font-weight: 600;
}}

QLabel#StepHeading {{
    font-size: 1.000em;
    font-weight: 600;
    color: {TEXT};
}}

QFrame#Card QLabel {{
    background: transparent;
}}

QFrame#Card QTableWidget {{
    background: transparent;
    border: none;
}}

QPushButton {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 0.53em 1.07em;
    font-size: 1.000em;
    color: {TEXT};
}}

QPushButton:hover {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT};
}}

QPushButton:disabled {{
    color: {TEXT_FAINT};
    border-color: {BORDER};
}}

QPushButton#Primary {{
    background-color: {ACCENT};
    border: none;
    color: {ON_ACCENT};
    font-weight: 600;
}}

QPushButton#Primary:hover {{
    background-color: {ACCENT_HOVER};
}}

QPushButton#Primary:disabled {{
    background-color: {BORDER};
    color: {TEXT_FAINT};
}}

QLineEdit, QComboBox, QSpinBox {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 0.47em 0.67em;
    font-size: 1.000em;
    selection-background-color: {ACCENT};
}}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}

/* A field opened inside a table cell (the count in the import's apply table)
   gets the cell's height, which is sized for one line of text. With the field
   padding above it needed 54px in a 35px cell at 14pt and showed the top of
   the number only. In a cell, the cell's own padding is enough. */
QAbstractItemView QLineEdit, QAbstractItemView QSpinBox {{
    padding: 0 0.3em;
    border-radius: 0;
}}

/* The drop-down part and its arrow follow the text. Left to Qt they were 16px
   wide at every size, a dot of an arrow with a dark stripe beside it at 14 and
   20pt (audit B4). No border of its own: the field's border is the only line. */
QComboBox {{
    padding-right: {_arrow_box}px;
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: {_arrow_box}px;
    border: none;
    background: transparent;
}}

QComboBox::down-arrow {{
    image: {_image("down", TEXT_DIM, _arrow_w, _arrow_h)};
    width: {_arrow_w}px;
    height: {_arrow_h}px;
}}

/* The list a combo opens: room inside the frame and a row that is a target. */
QComboBox QAbstractItemView {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    padding: {_edge}px;
    outline: none;
    selection-background-color: {ACCENT_DIM};
    selection-color: {TEXT};
}}

QComboBox QAbstractItemView::item {{
    padding: 0.30em 0.67em;
    min-height: 1.30em;
}}

/* Up and down buttons on the right edge, each half the field's height, with
   arrows of the theme's colour. Qt's own were two slits. */
QSpinBox {{
    padding-right: {_spin_w}px;
}}

QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    width: {_spin_w}px;
    background: {BG_CARD};
    border: none;
    border-left: 1px solid {BORDER};
}}

QSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: 5px;
}}

QSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-top: 1px solid {BORDER};
    border-bottom-right-radius: 5px;
}}

QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background: {ACCENT_DIM};
}}

QSpinBox::up-arrow {{
    image: {_image("up", TEXT_DIM, _spin_arrow, max(4, _spin_arrow // 2 + 1))};
    width: {_spin_arrow}px;
    height: {max(4, _spin_arrow // 2 + 1)}px;
}}

QSpinBox::down-arrow {{
    image: {_image("down", TEXT_DIM, _spin_arrow, max(4, _spin_arrow // 2 + 1))};
    width: {_spin_arrow}px;
    height: {max(4, _spin_arrow // 2 + 1)}px;
}}

/* The tabs of a tab widget are the navigation's mark again, turned on its
   side: the chosen one carries an accent line, the rest are text. */
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {BORDER};
}}

QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    border: none;
    border-bottom: {_edge}px solid transparent;
    padding: 0.55em 1.1em;
    margin-right: 0.2em;
}}

QTabBar::tab:selected {{
    color: {ACCENT};
    border-bottom: {_edge}px solid {ACCENT};
}}

QTabBar::tab:hover:!selected {{
    color: {TEXT};
    border-bottom: {_edge}px solid {BORDER};
}}

/* A message box wraps its text at whatever width the first line happens to
   have; with a floor it is a sentence, not a column (audit C8). In pixels,
   worked out from the text size: Qt's `em` in a size is the font's line
   height (37px at 20pt, measured), so 26em was 962px and wider than the box. */
QMessageBox QLabel#qt_msgbox_label {{
    min-width: {px(24)}px;
}}

/* A table is a list, not a spreadsheet.

   Every screen in this application was dominated by bordered grids: a frame
   round the whole thing, a line between every cell, a filled header, and
   banded rows underneath. Four kinds of line to separate rows that are already
   separated by being on different lines. Looking at two of these pages made
   the verdict plain: it did not read as modern, because the chrome was doing
   the talking.

   What is left: no outer frame, no vertical rules, no fill, and rows given
   room instead. Separation comes from space, which is what separates them on
   a printed page too. */
QListWidget, QTreeWidget, QTableWidget {{
    background: transparent;
    alternate-background-color: transparent;
    border: none;
    font-size: 1.000em;
    outline: none;
}}

QTableWidget {{
    gridline-color: transparent;
}}

QTreeWidget::item, QTableWidget::item {{
    padding: {_row_pad}px {_cell_pad}px;
    border: none;
    border-bottom: 1px solid {BG_ELEVATED};
}}

/* A list of names to pick from has the rows a table has: cell padding
   around the text, so a row is 1.2 lines and not 1 (audit C2). Not on every
   list - a list that holds widgets (the import review) sizes its rows by them. */
QListWidget#PickList::item {{
    padding: {_row_pad}px {_cell_pad}px;
}}

/* The same left edge as everything else, rather than a filled bar. */
QTreeWidget::item:selected, QTableWidget::item:selected,
QListWidget::item:selected {{
    background-color: {BG_ELEVATED};
    color: {TEXT};
}}

QTreeWidget::item:hover, QTableWidget::item:hover {{
    background-color: {BG_ELEVATED};
}}

/* Inside a card the same rule darkens instead of lifting: a card is lighter
   than the page, and the page's hover colour sits between them. Hover has to
   mean "up" wherever it happens. */
QFrame#Card QTreeWidget::item:hover, QFrame#Card QTableWidget::item:hover,
QGroupBox QTableWidget::item:hover {{
    background-color: {CARD_HOVER};
}}

/* A column heading is a label, not a title bar: no fill, no box, and small
   enough that it does not compete with the rows it names. */
QHeaderView {{
    background: transparent;
}}

QHeaderView::section {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: {DISPLAY_FAMILY};
    letter-spacing: 1.2px;
    text-transform: uppercase;
    font-size: 0.780em;
    font-weight: 600;
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: {_row_pad}px {_cell_pad}px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: {_scroll}px;
}}

QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: {_scroll // 2 - 1}px;
    min-height: {_scroll * 3}px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* The horizontal bar is the vertical one turned: without a rule it was Qt's
   own light frame with a corner square (audit B6). */
QScrollBar:horizontal {{
    background: transparent;
    height: {_scroll}px;
}}

QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: {_scroll // 2 - 1}px;
    min-width: {_scroll * 3}px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

QProgressBar {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    color: {TEXT};
}}

QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}

QToolButton#FilterChip {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 13px;
    padding: 0.33em 0.93em;
    font-size: 0.880em;
    color: {TEXT_DIM};
}}

QToolButton#FilterChip:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: {ON_ACCENT};
}}

QToolButton#FilterChip:hover:!checked {{
    border-color: {ACCENT};
}}

/* A group is a panel with its heading inside it, not a box with its title cut
   into the border line. The line-through-the-title is Qt's default and it is
   the single detail that dates an interface fastest: it draws attention to the
   frame rather than to what is inside it.

   Inside rather than above, because the cards built by hand elsewhere in this
   interface put their headings inside - and two kinds of section heading on
   two pages of one application is worse than either kind everywhere. */
QGroupBox {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-left: {_edge}px solid {BORDER};
    border-top-left-radius: 0px;
    border-bottom-left-radius: 0px;
    border-top-right-radius: {_r_card}px;
    border-bottom-right-radius: {_r_card}px;
    margin-top: 0px;
    padding: 2.30em 1.10em 1.10em 1.10em;
    font-family: {DISPLAY_FAMILY};
    font-size: {TYPE["section"]:.3f}em;
    font-weight: 600;
    color: {TEXT};
}}

QGroupBox::title {{
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 1.10em;
    top: 0.90em;
    padding: 0;
    background: transparent;
}}

QGroupBox#ReviewCard {{
    padding: 1.60em 0.70em 0.60em 0.70em;
    font-size: 0.880em;
    font-weight: 500;
    color: {TEXT_DIM};
}}

QGroupBox#ReviewCard::title {{
    left: 0.70em;
    top: 0.55em;
}}

QRadioButton, QCheckBox {{
    font-size: 1.000em;
    spacing: {_gap}px;
    background: transparent;
    /* Qt's em in a size is the line height of the font, so this is 1.25
       lines: a box with a label is hit without aiming (audit C2). */
    min-height: 1.25em;
}}

/* Fusion draws its own indicators, and against a dark panel they come out as
   a grey lozenge with a dot that is hard to see at any size - which on the
   inventory page meant neither of two mutually exclusive choices looked
   chosen. Drawn here instead, at a size that follows the text. */
QRadioButton::indicator, QCheckBox::indicator {{
    width: {_mark}px;
    height: {_mark}px;
    border: 2px solid {TEXT_FAINT};
    background: {BG_ELEVATED};
}}

/* In pixels, not em. Qt resolves `em` for font-size and silently ignores it
   for border-radius, so a radio button written in em came out square - two
   mutually exclusive choices, one drawn as a circle and one as a box. */
QRadioButton::indicator {{
    border-radius: {_mark_half}px;
}}

QCheckBox::indicator {{
    border-radius: 4px;
}}

QTableWidget QCheckBox::indicator {{
    width: {_mark_row}px;
    height: {_mark_row}px;
}}

QRadioButton::indicator:hover, QCheckBox::indicator:hover {{
    border-color: {TEXT_DIM};
}}

/* Chosen: a thicker ring, but the same outside. The width and height of an
   indicator are the inside of its border, so a ring of 6px on an inside of
   20px came out 32px against the 24px of the unchosen one - and 37 against 27
   at 20pt, where the radius, worked out for 27, turned it into a rounded
   square (audit B5). The inside shrinks by what the ring adds. */
QRadioButton::indicator:checked {{
    border: {_mark_ring}px solid {ACCENT};
    background: {BG};
    width: {max(2, _mark + 4 - 2 * _mark_ring)}px;
    height: {max(2, _mark + 4 - 2 * _mark_ring)}px;
}}

/* A tick, so chosen and not chosen differ by a mark and not only by a fill
   (audit C5). */
QCheckBox::indicator:checked {{
    border-color: {ACCENT};
    background: {ACCENT};
    image: {_image("check", ON_ACCENT, _mark)};
}}

QTableWidget QCheckBox::indicator:checked {{
    image: {_image("check", ON_ACCENT, _mark_row)};
}}

QRadioButton:disabled, QCheckBox:disabled {{
    color: {TEXT_FAINT};
}}

QSplitter::handle {{
    background-color: {BORDER};
}}
"""


# Applied at import so anything reading theme.GOOD before the application has
# chosen a theme still gets a real colour rather than an empty string.
STYLESHEET = apply(DEFAULT_THEME)
