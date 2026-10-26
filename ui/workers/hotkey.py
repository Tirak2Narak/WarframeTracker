"""evdev hotkey listener: the hotkey is the primary trigger.

Pattern taken from an earlier, working standalone hotkey script: evdev with
`selectors` for a non-blocking multi-device read loop, periodic hot-plug
rescan, and a focus gate so the key is inert everywhere except the game.

The focus gate matters here for a different reason than the rule against
stealing focus: it is about not firing a capture while the user is
alt-tabbed into a browser. `xdotool` gives the focused window's class
cheaply enough to check on every keypress.

The key and the window class are read from the configuration on every pass,
not once at start: choosing a key on the Setup page changes the configuration
and the listener takes it up within a pass, without a restart. `KeyCaptureWorker`
is the other half of that page: it reports which key was pressed, on any
readable input device and without the focus gate, so a newcomer can choose a key
by pressing it and then check that it arrives.
"""
from __future__ import annotations

import selectors
import subprocess
import time

from PySide6.QtCore import QThread, Signal

from wft.capture import keys, window
from wft.capture.keys import ecodes, evdev  # noqa: F401 - None when not installed

RESCAN_INTERVAL = 5.0


class FocusGate:
    """The focus check of the hotkey, shared with the click reading: one
    function decides "the game has the focus", so the two cannot disagree."""

    def __init__(self):
        self._cache: tuple[float, bool] = (0.0, False)

    def matches(self, window_class: str) -> bool:
        """Whether the focused window belongs to the game.

        Two commands rather than one, and not by preference: on KDE Wayland
        `xdotool getwindowclassname` fails with "XGetClassHint[WM_CLASS]
        failed" even while the game is focused, so the gate never opened and
        the hotkey never fired. `xdotool` finds the window fine — it is only its
        own class lookup that breaks. `xprop` reads the same property without
        trouble.

        This is the approach the earlier standalone script takes, which is
        why that one worked where this did not.

        A failure to determine focus is treated as "not in the game": firing a
        capture while the user is in a browser is worse than missing one, and
        the manual button covers the miss.
        """
        if not window_class:
            return True
        now = time.monotonic()
        if now - self._cache[0] < 0.25:
            return self._cache[1]

        matched = False
        try:
            found = subprocess.run(["xdotool", "getwindowfocus"],
                                   capture_output=True, text=True, timeout=1)
            window_id = found.stdout.strip()
            if window_id.isdigit():
                # Class and title both, because find_game() matches on either
                # and one configured value has to serve both. Under Steam the
                # class is the app id (steam_app_230410) while the title is
                # "Warframe", so checking only the class rejects the sensible
                # setting of "warframe".
                properties = subprocess.run(
                    ["xprop", "-id", window_id, "WM_CLASS", "WM_NAME"],
                    capture_output=True, text=True, timeout=1)
                values = {"WM_CLASS": "", "WM_NAME": ""}
                for line in properties.stdout.splitlines():
                    name, sep, value = line.partition("=")
                    prop = name.split("(")[0].strip()
                    if sep and prop in values:
                        values[prop] = value.strip()
                matched = window.is_game(values["WM_CLASS"], values["WM_NAME"],
                                         window_class)
        except (OSError, subprocess.TimeoutExpired):
            matched = False

        self._cache = (now, matched)
        return matched


class HotkeyWorker(QThread):
    """Emits `triggered` when the configured key is pressed while the game
    has focus. Silently inert if evdev is unavailable or nothing is
    configured — the manual trigger button still works either way.
    """

    triggered = Signal()
    #: Each firing, with the device it came from (`hotkey: F23 from <name>`),
    #: sent just before `triggered`.
    fired = Signal(str)
    status = Signal(str)

    def __init__(self, cfg, parent=None):
        # Focus is checked on every keypress; caching briefly keeps that from
        # spawning two processes per keystroke during normal play.
        self._gate = FocusGate()
        super().__init__(parent)
        self.cfg = cfg

    def _focus_matches(self, window_class: str) -> bool:
        """Whether the focused window belongs to the game (see `FocusGate`)."""
        return self._gate.matches(window_class)

    def _register_devices(self, selector: selectors.BaseSelector, registered: set):
        try:
            for device in keys.key_devices(registered):
                selector.register(device, selectors.EVENT_READ, data=device)
                registered.add(device.path)
        except Exception as exc:  # noqa: BLE001
            self.status.emit(f"evdev: cannot list devices ({exc})")

    def run(self) -> None:  # noqa: C901 - one small event loop, kept together
        if evdev is None:
            self.status.emit("evdev not installed — hotkey disabled, use the manual button")
            return

        selector = selectors.DefaultSelector()
        registered: set = set()
        #: What the configuration said on the last pass, so a change is noticed
        #: (and announced) once and a steady state is not announced again.
        key_name: str | None = None
        target_code: int | None = None
        announced = True
        last_scan = float("-inf")
        try:
            while not self.isInterruptionRequested():
                wanted = str(self.cfg.get("hotkey.key", "") or "").strip()
                if wanted != key_name:
                    key_name = wanted
                    target_code = None
                    announced = True
                    if not key_name:
                        self.status.emit("no hotkey configured")
                    else:
                        target_code = keys.key_code(key_name)
                        if target_code is None:
                            self.status.emit(f"unknown key name {key_name!r}")
                        else:
                            announced = False
                window_class = str(self.cfg.get("game.window_class", ""))

                # Devices are opened once there is a key to listen for, and
                # looked for again now and then (a keyboard plugged in later).
                now = time.monotonic()
                if target_code is not None and now - last_scan > RESCAN_INTERVAL:
                    self._register_devices(selector, registered)
                    last_scan = now
                if not announced:
                    announced = True
                    if not registered:
                        self.status.emit("no readable input devices — is this "
                                         "account in the 'input' group?")
                    else:
                        self.status.emit(f"listening for {key_name}")

                for selector_key, _mask in selector.select(timeout=0.5):
                    device = selector_key.data
                    try:
                        for event in device.read():
                            if (target_code is not None
                                    and event.type == ecodes.EV_KEY
                                    and event.code == target_code
                                    and event.value == 1
                                    and self._focus_matches(window_class)):
                                self.fired.emit(
                                    f"hotkey: {key_name} from "
                                    f"{getattr(device, 'name', None) or device.path}")
                                self.triggered.emit()
                    except OSError:
                        try:
                            selector.unregister(device)
                            registered.discard(device.path)
                        except KeyError:
                            pass
        finally:
            for selector_key in list(selector.get_map().values()):
                try:
                    selector_key.data.close()
                except OSError:
                    pass
            selector.close()


class KeyCaptureWorker(QThread):
    """Reports each key that goes down, on any readable input device.

    For choosing the hotkey by pressing it, and for checking that it arrives.
    No focus gate and no configured key: the question here is "what did the
    keyboard send", whichever window has the focus. Mouse buttons are not
    reported (they have no key name, and the click on the button that starts
    the choosing would otherwise be the first thing heard).

    `status` says why nothing can be heard: python-evdev missing, or no input
    device this account may read, which is what a missing `input` group looks
    like. The worker ends itself in that case rather than waiting for a key
    that cannot come. Otherwise it runs until interrupted or `timeout` seconds
    have passed (0 means no limit).
    """

    pressed = Signal(str)
    status = Signal(str)

    def __init__(self, timeout: float = 0.0, parent=None):
        super().__init__(parent)
        self.timeout = timeout

    def run(self) -> None:
        if evdev is None:
            self.status.emit("python-evdev is not installed, so keys cannot be read")
            return
        selector = selectors.DefaultSelector()
        registered: set = set()
        try:
            try:
                for device in keys.key_devices():
                    selector.register(device, selectors.EVENT_READ, data=device)
                    registered.add(device.path)
            except Exception as exc:  # noqa: BLE001
                self.status.emit(f"cannot list input devices ({exc})")
                return
            if not registered:
                self.status.emit("no readable input device — is this account "
                                 "in the 'input' group?")
                return
            deadline = time.monotonic() + self.timeout if self.timeout else None
            while not self.isInterruptionRequested():
                if deadline is not None and time.monotonic() > deadline:
                    break
                for selector_key, _mask in selector.select(timeout=0.2):
                    device = selector_key.data
                    try:
                        for event in device.read():
                            if event.type == ecodes.EV_KEY and event.value == 1:
                                name = keys.key_name(event.code)
                                if name:
                                    self.pressed.emit(name)
                    except OSError:
                        try:
                            selector.unregister(device)
                        except KeyError:
                            pass
        finally:
            for selector_key in list(selector.get_map().values()):
                try:
                    selector_key.data.close()
                except OSError:
                    pass
            selector.close()
