"""Choosing the hotkey by pressing it, and checking that it arrives.

One widget for the Setup page and for Settings -> Capture, so the two cannot
drift apart: a button "Choose a key..." that waits for the next key pressed on
any readable input device, saves its name and applies it to the running
program, and a second mode that waits for the saved key and says "Received".

The name comes from `wft.capture.keys.key_name`, the inverse of the lookup the
listener uses, so whatever is shown here is what gets saved and what is heard.
Typing an evdev name was where a newcomer stuck: `\\` is not one, `BACKSLASH` is.

Reading the keys is the listener's own mechanism (evdev, the same device list).
Without access to the input devices nothing can be read and the widget says so,
rather than waiting for a key that cannot come.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ui import settings_io, theme
from ui.workers.hotkey import KeyCaptureWorker
from wft.capture import keys

#: How long a choice or a check waits for a key before giving up. Long enough to
#: find the key, short enough that a forgotten page does not read the keyboard
#: for the rest of the session.
WAIT_SECONDS = 30

#: The key that cancels a choice. It cannot be chosen as the hotkey, which costs
#: nothing: Escape is a poor hotkey and every dialog teaches that it cancels.
CANCEL_KEY = "ESC"

#: A key that was pressed to activate this very button (Space, Enter) is released
#: after the choice has been made, and that release would press the button again.
#: Presses this soon after a choice are ignored.
SETTLE_SECONDS = 0.5


class KeyPicker(QWidget):
    #: The key name, after it was saved and applied.
    picked = Signal(str)
    #: True when the saved hotkey was heard in the check.
    received = Signal(str)

    def __init__(self, cfg, parent=None, with_check_button: bool = True):
        super().__init__(parent)
        self.cfg = cfg
        self._worker: KeyCaptureWorker | None = None
        #: None, "pick" or "check".
        self.mode: str | None = None
        self._settled_at = 0.0

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.px(0.8))
        self.choose_btn = QPushButton("Choose a key…")
        self.choose_btn.setObjectName("Primary")
        self.choose_btn.clicked.connect(self._choose_clicked)
        row.addWidget(self.choose_btn)
        self.check_btn = QPushButton("Test the key")
        self.check_btn.clicked.connect(self._check_clicked)
        self.check_btn.setVisible(with_check_button)
        row.addWidget(self.check_btn)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        row.addWidget(self.message, 1)
        self._sync_buttons()

    # -- buttons -----------------------------------------------------------
    def _choose_clicked(self) -> None:
        if time.monotonic() < self._settled_at:
            return
        if self.mode == "pick":
            self.cancel("Cancelled.")
        else:
            self.start_pick()

    def _check_clicked(self) -> None:
        if time.monotonic() < self._settled_at:
            return
        if self.mode == "check":
            self.cancel("")
        else:
            self.start_check()

    def _sync_buttons(self) -> None:
        self.choose_btn.setText("Cancel" if self.mode == "pick" else "Choose a key…")
        self.check_btn.setText("Stop testing" if self.mode == "check" else "Test the key")
        self.check_btn.setEnabled(self.mode == "check" or (
            self.mode is None and bool(self._hotkey())))

    def _hotkey(self) -> str:
        return str(self.cfg.get("hotkey.key", "") or "").strip()

    def _say(self, text: str, colour: str | None = None) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {colour or theme.TEXT_DIM};")

    # -- the two modes -----------------------------------------------------
    def start_pick(self) -> None:
        self._begin("pick")
        self._say(f"Press the key you want to use, now. {CANCEL_KEY.capitalize()} cancels.",
                  theme.ACCENT)

    def start_check(self) -> None:
        if not self._hotkey():
            self._say("Choose a key first.", theme.WARN)
            return
        self._begin("check")
        self._say(f"Press {self._hotkey()} now to check that it arrives.", theme.ACCENT)

    def _begin(self, mode: str) -> None:
        self._stop_worker()
        self.mode = mode
        worker = KeyCaptureWorker(timeout=WAIT_SECONDS)
        worker.pressed.connect(self._on_pressed)
        worker.status.connect(self._on_problem)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._worker = worker
        self._sync_buttons()
        worker.start()

    def cancel(self, text: str = "") -> None:
        self._stop_worker()
        self.mode = None
        self._settled_at = time.monotonic() + SETTLE_SECONDS
        self._say(text)
        self._sync_buttons()

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            for signal in (worker.pressed, worker.status):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            worker.requestInterruption()
            worker.wait(2000)

    # -- what the worker says ----------------------------------------------
    def _on_pressed(self, name: str) -> None:
        if self.mode == "pick":
            if name == CANCEL_KEY:
                self.cancel("Cancelled.")
                return
            self._save(name)
        elif self.mode == "check":
            if name == self._hotkey():
                self._stop_worker()
                self.mode = None
                self._settled_at = time.monotonic() + SETTLE_SECONDS
                self._say(f"Received {name}. This key arrives.", theme.GOOD)
                self._sync_buttons()
                self.received.emit(name)
            else:
                self._say(f"That was {name}; the hotkey is {self._hotkey()}. "
                          f"Press {self._hotkey()}.", theme.WARN)

    def _save(self, name: str) -> None:
        try:
            settings_io.save_value(self.cfg, "hotkey", "key", name)
        except OSError as exc:
            self.cancel("")
            self._say(f"Could not save the key: {exc}", theme.BAD)
            return
        self._stop_worker()
        self.mode = None
        self._settled_at = time.monotonic() + SETTLE_SECONDS
        self._sync_buttons()
        self.picked.emit(name)
        # Straight into the check: pressing the key once more is the proof that
        # it arrives, and the user is holding it already.
        self.start_check()
        self._say(f"{name} is saved. Press it now to check that it arrives.",
                  theme.ACCENT)

    def _on_problem(self, text: str) -> None:
        self.cancel("")
        self._say(text + " Setup, \"Keyboard access\", says what to do.", theme.BAD)

    def _on_worker_finished(self, worker) -> None:
        # Only the current worker's end counts; a replaced one finishes late.
        if worker is not self._worker:
            return
        self._worker = None
        if self.mode is not None:
            was = self.mode
            self.mode = None
            self._say("No key was pressed. Try again." if was == "pick"
                      else "No key was pressed. Press \"Test the key\" to try again.",
                      theme.WARN)
            self._sync_buttons()

    # -- lifecycle -----------------------------------------------------------
    def refresh(self) -> None:
        self._sync_buttons()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        # A page that is not on screen is not waiting for anybody's key.
        if self.mode is not None:
            self.cancel("")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._stop_worker()
        super().closeEvent(event)
