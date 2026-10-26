"""The colours of the reward overlay, worked out from the configuration.

Tracker side only. The helper process draws with the two colours it is sent and
does not import this module: it needs `ui.theme`, whose import writes a glyph
cache and which the helper (on xcb, without the tracker's themes) must not
load. `ui.theme` is therefore imported inside the functions, never at the top.

`[overlay] colours` is one of "red-green", "blue-orange", the name of an app
theme that reads well on a light picture, or "custom" (then
`[overlay] custom_frame` and `[overlay] custom_text`). One function,
`look_from_config`, turns that into `{"frame": "#rrggbb", "text": "#rrggbb"}`;
the Overlays page, its test button and the real overlay all call it.
"""
from __future__ import annotations

from PySide6.QtGui import QColor

from ui.overlay import drawing

#: The two fixed presets: the first sketch, and the colour-blind safe pair
#: (Okabe-Ito orange and sky blue, on the blue-yellow axis red-green colour
#: blindness keeps).
FIXED = {
    "red-green": {"frame": "#ff2a2a", "text": "#3ddc6b"},
    "blue-orange": {"frame": "#e69f00", "text": "#56b4e9"},
}
CUSTOM = "custom"
#: The least contrast of the text over the dark box on a white ground (WCAG, large text).
MIN_CONTRAST = 3.0


def _luminance(rgb) -> float:
    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(text: str, ground: int = 255) -> float:
    """The WCAG contrast of the colour `text` over the overlay's dark box, which
    lies over a grey `ground` (255: the lightest picture, 0: the darkest)."""
    colour = QColor(text)
    alpha = drawing.BOX_RGBA[3] / 255
    shown = [alpha * c + (1 - alpha) * ground for c in drawing.BOX_RGBA[:3]]
    lt, lb = _luminance((colour.red(), colour.green(), colour.blue())), _luminance(shown)
    return (max(lt, lb) + 0.05) / (min(lt, lb) + 0.05)


def theme_presets() -> dict[str, dict[str, str]]:
    """One preset per app theme whose "good" colour reads well on a light
    picture: frame = the theme's accent, text = its good colour."""
    from ui import theme

    return {name: {"frame": palette["accent"], "text": palette["good"]}
            for name, palette in theme.THEMES.items()
            if contrast(palette["good"]) >= MIN_CONTRAST}


def presets() -> dict[str, dict[str, str]]:
    """Every preset by its config name, the fixed ones first."""
    return {**FIXED, **theme_presets()}


def valid_hex(value) -> str | None:
    """`value` as lowercase "#rrggbb", or None if it is not such a colour."""
    text = str(value).strip() if value is not None else ""
    if len(text) != 7 or not text.startswith("#"):
        return None
    colour = QColor(text)
    return colour.name() if colour.isValid() else None


def custom_from_config(cfg) -> dict:
    """The saved custom colours, `[overlay] custom_frame` and `custom_text`, as
    two "#rrggbb" values, whatever `colours` says; an invalid one reads as
    red-green. The page's fields show this, the overlay draws with it."""
    return drawing.merge_look({"frame": valid_hex(cfg.get("overlay.custom_frame")),
                               "text": valid_hex(cfg.get("overlay.custom_text"))})


def look_from_config(cfg) -> dict:
    """The look `[overlay] colours` (and the custom colours) ask for, as two
    "#rrggbb" values; anything unknown or invalid reads as red-green."""
    name = cfg.get("overlay.colours")
    if name == CUSTOM:
        return custom_from_config(cfg)
    return drawing.merge_look(presets().get(name))
