"""Hotkey name resolution and input-device access, shared by the listener and
`wf doctor`.

Kept out of `ui.workers.hotkey` because that module needs Qt and the doctor
has to run when Qt is the thing that is missing. Both ask the same questions
- what key code is "F23", which devices may I read - and answering them in two
places is how the listener and its diagnosis come to disagree.
"""
from __future__ import annotations

from typing import Iterator

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover - depends on what is installed
    evdev = None
    ecodes = None


def key_code(name: str) -> int | None:
    """evdev key code for a configured name ("F23", "KP1", "KEY_F23"), or None."""
    if ecodes is None:
        return None
    name = name.strip().upper()
    code_name = name if name.startswith("KEY_") else f"KEY_{name}"
    code = getattr(ecodes, code_name, None)
    return code if isinstance(code, int) else None


def key_name(code: int) -> str | None:
    """The configurable name for a key code ("F8", "BACKSLASH"), or None.

    The reverse of `key_code`, used when the user presses a key to choose it.
    Only names `key_code` accepts come back, so what is shown, what is saved
    and what the listener later looks up are one and the same. Mouse buttons
    (BTN_LEFT and the like) have no KEY_ name and give None: a click on the
    button that starts the choosing must not become the hotkey.
    """
    if ecodes is None:
        return None
    names = ecodes.KEY.get(code)
    if isinstance(names, (list, tuple)):
        names = next((n for n in names if n.startswith("KEY_")), None)
    if not isinstance(names, str) or not names.startswith("KEY_"):
        return None
    short = names[len("KEY_"):]
    return short if key_code(short) == code else None


def key_devices(skip: set | frozenset = frozenset()) -> Iterator:
    """Readable input devices that can send key events, as opened devices.

    Paths in `skip` are not opened again. A device node the account may not
    read is passed over silently, which is how a missing `input` group looks.
    The caller owns (and closes) what it takes.
    """
    for path in evdev.list_devices():
        if path in skip:
            continue
        try:
            device = evdev.InputDevice(path)
            if ecodes.EV_KEY not in device.capabilities():
                device.close()
                continue
        except (OSError, PermissionError):
            continue
        yield device


def pointer_devices(skip: set | frozenset = frozenset()) -> Iterator:
    """Readable input devices that have a left mouse button (BTN_LEFT), opened.

    Same rules as `key_devices`; a keyboard has key events but no mouse button
    and is passed over. Only for the click-to-pick reading, which opens
    these while the reward overlay stands and closes them after.
    """
    for path in evdev.list_devices():
        if path in skip:
            continue
        try:
            device = evdev.InputDevice(path)
            if ecodes.BTN_LEFT not in device.capabilities().get(ecodes.EV_KEY, []):
                device.close()
                continue
        except (OSError, PermissionError):
            continue
        yield device
