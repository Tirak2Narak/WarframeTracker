"""Remembering where the user put the windows.

Both windows are dragged into place once and should stay there. Qt stores the
geometry; what actually survives depends on the session:

* **Size** is restored reliably everywhere.
* **Position** is restored under X11 and XWayland. Under a native Wayland
  session the compositor owns window placement and ignores a client's request,
  by design — a Wayland client is not allowed to place itself.

So the position is saved and applied on a best-effort basis, and where Wayland
refuses it, a KWin window rule does the job instead (see docs/SETUP.md). Saying
that plainly beats an application that appears to forget what it was told.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings

ORGANISATION = "warframe-tracker"
APPLICATION = "warframe-tracker"


def _settings() -> QSettings:
    return QSettings(ORGANISATION, APPLICATION)


def settings_file() -> str:
    """Where Qt keeps the stored geometry (warframe-tracker.conf)."""
    return _settings().fileName()


def save(window, key: str) -> None:
    """Store one window's geometry under `key`."""
    settings = _settings()
    settings.setValue(f"{key}/geometry", window.saveGeometry())
    settings.sync()


def restore(window, key: str) -> bool:
    """Apply a stored geometry. False when nothing was stored yet."""
    stored = _settings().value(f"{key}/geometry")
    if not isinstance(stored, QByteArray) or stored.isEmpty():
        return False
    return bool(window.restoreGeometry(stored))


# -- the palette grid overlay -------------------------------------------
#
# Where the user put the grid over the colour palette, in desktop pixels: x, y,
# width and height of the grid without its margin. Written only by the tracker,
# and only when the overlay helper reports a position the user locked.

PALETTE_KEYS = ("x", "y", "w", "h")

#: An earlier standalone script kept its position here.
LEGACY_PALETTE_FILE = ("warframe", "paletten-overlay.json")


def _legacy_palette():
    """The position the old script stored (keys x, y, b, h), or None."""
    import json
    from pathlib import Path

    path = Path.home().joinpath(".config", *LEGACY_PALETTE_FILE)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        values = {"x": stored["x"], "y": stored["y"], "w": stored["b"], "h": stored["h"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values.values()):
        return None
    return values if values["w"] > 0 and values["h"] > 0 else None


def stored_palette() -> dict | None:
    """The stored grid position `{x, y, w, h}`, or None. Reads only."""
    settings = _settings()
    try:
        values = {key: int(settings.value(f"palette/{key}")) for key in PALETTE_KEYS}
    except (TypeError, ValueError):
        return None
    return values if values["w"] > 0 and values["h"] > 0 else None


def palette_position() -> dict | None:
    """`stored_palette`, after taking over the old script's position once.

    Once, when nothing was ever stored, the position of the old script is taken
    over; `palette/legacy_checked` makes sure that happens only once, so
    "Reset position" is not undone by the old file. This writes; call it where
    the grid is about to be shown, not while a page is only being built.
    """
    settings = _settings()
    if not settings.value("palette/legacy_checked", False, type=bool):
        settings.setValue("palette/legacy_checked", True)
        legacy = _legacy_palette()
        if legacy is not None and stored_palette() is None:
            save_palette(legacy)
        settings.sync()
    return stored_palette()


def save_palette(position: dict) -> None:
    settings = _settings()
    for key in PALETTE_KEYS:
        settings.setValue(f"palette/{key}", int(position[key]))
    settings.sync()


def clear_palette() -> None:
    settings = _settings()
    for key in PALETTE_KEYS:
        settings.remove(f"palette/{key}")
    settings.setValue("palette/legacy_checked", True)
    settings.sync()


# -- the newest release the user has seen in "What's new" ----------------------

WHATSNEW_KEY = "whatsnew/seen"


def whatsnew_seen() -> str:
    """The release number last acknowledged, "" when there is none."""
    value = _settings().value(WHATSNEW_KEY)
    return str(value) if value else ""


def set_whatsnew_seen(version: str) -> None:
    settings = _settings()
    settings.setValue(WHATSNEW_KEY, version)
    settings.sync()
