"""Reads the left click on a reward card while the reward overlay stands.

The player confirms a pick by clicking the card in the game. This worker is the
reading half: it reports where the pointer was at each left click, and
`ui.overlay.rewards.RewardOverlay.on_click` turns that into a card.

What keeps it narrow:

* Mouse devices are opened only while `Arming` is set (overlay up, switch on),
  and closed again when it clears. Disarmed, the thread sleeps on a pipe; there
  is no timeout and no polling.
* Only BTN_LEFT going down, only while the game has the focus (the hotkey's
  `FocusGate`), only the pointer position is read (`XQueryPointer`). Nothing is
  stored, nothing is grabbed (no EVIOCGRAB): the click reaches the game as it
  always did. No input is sent. The log gets one word per click (`click: card 3`,
  `click: outside the cards`, `click: game not focused`), never a position.
"""
from __future__ import annotations

import selectors

from PySide6.QtCore import QThread, Signal

from wft.capture import keys, window
from wft.capture.keys import ecodes, evdev  # noqa: F401 - None when not installed
from ui.workers.hotkey import FocusGate


class PickClickWorker(QThread):
    #: A left click in the game window, at (x, y) in root-window coordinates.
    clicked = Signal(int, int)
    status = Signal(str)
    #: A click that was not counted, as a log line (`click: game not focused`).
    ignored = Signal(str)

    def __init__(self, cfg, arming, focus=None, pointer=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.arming = arming
        #: `focus(window_class) -> bool`, `pointer() -> (x, y)`; tests replace them.
        self._focus = focus or FocusGate().matches
        self._pointer = pointer or window.pointer_position

    def stop(self) -> None:
        self.requestInterruption()
        self.arming.wake()
        self.wait(2000)

    @staticmethod
    def _close(selector: selectors.BaseSelector, wake_fd) -> None:
        for selector_key in list(selector.get_map().values()):
            if selector_key.data is None:
                continue
            try:
                selector.unregister(selector_key.fileobj)
            except (KeyError, ValueError):
                pass
            try:
                selector_key.data.close()
            except OSError:
                pass

    def _open(self, selector: selectors.BaseSelector) -> int:
        count = 0
        try:
            for device in keys.pointer_devices():
                selector.register(device, selectors.EVENT_READ, data=device)
                count += 1
        except Exception as exc:  # noqa: BLE001
            self.status.emit(f"cannot list input devices ({exc})")
        if not count:
            self.status.emit("no readable mouse — is this account in the 'input' group?")
        return count

    def run(self) -> None:
        if evdev is None:
            self.status.emit("evdev not installed — clicks cannot be read")
            return
        selector = selectors.DefaultSelector()
        selector.register(self.arming, selectors.EVENT_READ, data=None)
        opened = False
        try:
            while not self.isInterruptionRequested():
                armed = self.arming.is_set()
                if armed and not opened:
                    self._open(selector)
                    opened = True
                elif not armed and opened:
                    self._close(selector, self.arming)
                    opened = False
                for selector_key, _mask in selector.select(timeout=None):
                    device = selector_key.data
                    if device is None:
                        self.arming.drain()
                        continue
                    try:
                        for event in device.read():
                            if (event.type == ecodes.EV_KEY
                                    and event.code == ecodes.BTN_LEFT
                                    and event.value == 1):
                                self._on_left_down()
                    except OSError:
                        try:
                            selector.unregister(device)
                        except KeyError:
                            pass
        finally:
            self._close(selector, self.arming)
            selector.close()

    def _on_left_down(self) -> None:
        # The flag again, per event: a click read just after the overlay ended.
        if not self.arming.is_set():
            return
        if not self._focus(str(self.cfg.get("game.window_class", ""))):
            self.ignored.emit("click: game not focused")
            return
        try:
            x, y = self._pointer()
        except Exception:  # noqa: BLE001 - no X, no position: no card
            return
        self.clicked.emit(int(x), int(y))
