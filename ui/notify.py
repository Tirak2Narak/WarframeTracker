"""The recommendation as a desktop notification.

After a reading the Rewards page already shows the answer, but with the game in
front the page is behind it. A normal desktop notification (`notify-send`, the
freedesktop notification service) reaches the user without Alt+Tab. It is not
an overlay and touches nothing of the game. It is the fallback of the
reward overlay: sent when the overlay was not shown.

Sending never blocks the GUI (a QProcess) and never fails loudly: a missing
`notify-send` or a failed call costs the notification and, at most, one line on
stderr per program run. A new message replaces the previous one (`--replace-id`)
instead of piling up.

Tests replace the sender with `set_sender`, so no real `notify-send` runs there.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QProcess

APP_NAME = "Warframe Tracker"
EXPIRE_MS = 15000
_ICON = Path(__file__).resolve().parent.parent / "packaging" / "warframe-tracker.svg"


class NotifySend(QObject):
    """Runs `notify-send` asynchronously and remembers the id of the last message."""

    def __init__(self, program: str = "notify-send", parent=None):
        super().__init__(parent)
        self.program = program
        self.last_id: int | None = None
        self._warned = False
        self._process: QProcess | None = None
        self._busy = False
        self._pending: tuple[str, str] | None = None

    def arguments(self, summary: str, body: str) -> list[str]:
        args = [f"--app-name={APP_NAME}", f"--expire-time={EXPIRE_MS}", "--print-id"]
        if self.last_id is not None:
            args.append(f"--replace-id={self.last_id}")
        if _ICON.is_file():
            args.append(f"--icon={_ICON}")
        return args + ["--", summary, body]

    def _warn(self, text: str) -> None:
        if not self._warned:
            self._warned = True
            print(f"warning: desktop notification not sent: {text}", file=sys.stderr)

    def send(self, summary: str, body: str = "") -> None:
        if self._busy:
            # One at a time, so the id of the first can replace itself in the
            # second; only the newest waiting message matters.
            self._pending = (summary, body)
            return
        if shutil.which(self.program) is None:
            self._warn(f"{self.program} not found")
            return
        if self._process is None:
            # One process object for the sender's whole life: deleting a
            # QProcess from inside its own signal crashed the test suite.
            self._process = QProcess(self)
            self._process.finished.connect(self._done)
            self._process.errorOccurred.connect(self._failed)
        self._busy = True
        self._process.setProgram(self.program)
        self._process.setArguments(self.arguments(summary, body))
        self._process.start()

    def _failed(self, _error) -> None:
        if self._busy and self._process.state() == QProcess.NotRunning:
            self._warn(self._process.errorString())
            self._release()

    def _done(self, code: int, _status=None) -> None:
        if not self._busy:
            return
        if code != 0:
            self._warn(f"{self.program} exited with {code}")
        else:
            out = bytes(self._process.readAllStandardOutput()).decode(errors="replace").strip()
            self.last_id = int(out) if out.isdigit() else self.last_id
        self._release()

    def _release(self) -> None:
        self._busy = False
        pending, self._pending = self._pending, None
        if pending is not None:
            self.send(*pending)


_sender: Callable[[str, str], None] | None = None


def set_sender(sender: Callable[[str, str], None] | None) -> None:
    """Replace what delivers a message (tests); None goes back to `notify-send`."""
    global _sender
    _sender = sender


def notify(summary: str, body: str = "") -> None:
    """Show one message. Never raises."""
    global _sender
    try:
        if _sender is None:
            _sender = NotifySend().send
        _sender(summary, body)
    except Exception as exc:  # noqa: BLE001 - a notification is never worth a crash
        print(f"warning: desktop notification not sent: {exc}", file=sys.stderr)
