"""The overlay helper: a separate process that owns the overlay windows.

    python3 -m ui.overlay.helper --socket NAME

Why a process of its own: the tracker runs natively on Wayland, and a Wayland
client cannot place a window above another program's window. The game runs as
an X11 window (XWayland), where it can. A Qt process has one platform, so the
windows that must lie over the game live here, on the `xcb` platform, which is
set before Qt loads (unless the caller chose one; the tests use `offscreen`).

The helper only draws. A locked overlay is transparent for the mouse and takes
no focus; it reads nothing from the game and sends it no input. The one
exception is the aligning mode of the overlay's own window, where it takes the
mouse and keys so the grid can be put in place.

Protocol: the tracker listens on a local socket and starts the helper with its
name; the helper connects. One JSON object per line, both ways.

Tracker to helper (`{"cmd": ...}`):

* `palette_show` with `position` (`{x, y, w, h}` of the grid without its margin,
  or null) and `style`. With a position the grid appears locked there; without
  one it opens in the aligning mode at a size worked out from the screen.
* `palette_hide`, `palette_unlock` (aligning), `palette_lock` (save and lock),
  `palette_style` with `style`, `quit`.
* `reward_show`: the recommendation over the reward screen. `window`
  (`{x, y, w, h}`, the game window in desktop coordinates, which the overlay
  covers exactly), `tiles` (the cards as fractions of the window, `[x, y, w, h]`
  each), `best` (index of the recommended card or null), `headline` (the words
  for "nothing to take", used when `best` is null), `lines` (per card a list of
  up to 3 info lines), `picked` (index of the card the player chose, or null;
  that card gets a tick), `look` (`frame` and `text`, each "#rrggbb"; anything
  that is not a colour is the red-green default), `token` (a number
  the helper hands back). The window
  is click-through and takes no focus, always.
* `reward_pick` with `picked` (index or null): set or clear the tick of a shown
  overlay. `reward_hide`.

Helper to tracker (`{"type": ...}`):

* `state`: `shown` (bool), `mode` (`hidden`, `locked`, `aligning`), `position`
  (null when hidden), `reason` (what caused it: `show`, `hide`, `unlock`,
  `lock`, `cancel`, `style`, `hello`). A `lock` is the one the user made (Enter,
  double click, the Lock button): the tracker stores that position.
* `reward`: the reward overlay's state, `shown` (bool), `reason` (`show`, `pick`,
  `hide`), `token` (of the last `reward_show`), `picked`.
* `error` with `message`.

The helper ends when the connection to the tracker ends, so a tracker that
crashed leaves no overlay behind, and only one helper runs per user (a lock file).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PySide6.QtCore import (  # noqa: E402
    QLockFile, QObject, QPoint, QRect, QStandardPaths, Qt, QTimer, Signal,
)
from PySide6.QtGui import QGuiApplication, QPainter  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from ui.overlay import drawing  # noqa: E402

#: Share of the screen height the grid gets when it opens without a position.
START_HEIGHT_SHARE = 0.7

#: Smallest the grid can be dragged to, besides the margin.
MIN_GRID = (50, 100)


def default_raster(style: dict, screen: QRect) -> QRect:
    """Where the grid opens without a stored position: centred, 70% of the
    screen's height, cells about as wide as they are high."""
    height = int(screen.height() * START_HEIGHT_SHARE)
    width = int(height * int(style["columns"]) / int(style["rows"]))
    return QRect(screen.center().x() - width // 2, screen.center().y() - height // 2,
                 width, height)


def _rect_from(position) -> QRect | None:
    """A QRect from `{x, y, w, h}`; None when it is not one."""
    try:
        x, y, w, h = (int(position[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    return QRect(x, y, w, h) if w > 0 and h > 0 else None


def _position_of(rect: QRect) -> dict:
    return {"x": rect.x(), "y": rect.y(), "w": rect.width(), "h": rect.height()}


class PaletteWindow(QWidget):
    """The grid window. Modes: hidden, locked (click-through, over everything),
    aligning (a tool window that takes mouse and keys)."""

    #: A reason string (see the module docstring) whenever the state changed.
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("warframe-tracker palette grid")
        self.style_ = drawing.merge_style(None)
        self.mode = "hidden"
        #: The last position the user locked (or the tracker handed over); Esc
        #: goes back to it.
        self.saved: QRect | None = None
        self._drag = None

    # -- state -----------------------------------------------------------
    def _margin(self) -> int:
        return drawing.margin_px(self.style_)

    def raster(self) -> QRect:
        """The grid without its margin: the rectangle the position means."""
        m = self._margin()
        return self.geometry().adjusted(m, m, -m, -m)

    def _place(self, raster: QRect) -> None:
        m = self._margin()
        self.setGeometry(raster.adjusted(-m, -m, m, m))

    def position(self) -> dict | None:
        return None if self.mode == "hidden" else _position_of(self.raster())

    def _apply_mode(self, mode: str) -> None:
        """Switch the window flags and show it. The geometry is kept."""
        if mode == self.mode and self.isVisible():
            self.update()
            return
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        if mode == "locked":
            flags |= (Qt.X11BypassWindowManagerHint | Qt.WindowTransparentForInput
                      | Qt.WindowDoesNotAcceptFocus)
        else:
            flags |= Qt.Tool
        geometry = self.geometry()
        self.setWindowFlags(flags)
        self.setGeometry(geometry)
        self.mode = mode
        self.show()
        if mode == "aligning":
            self.raise_()
            self.activateWindow()
            self.setFocus()
        self.update()

    # -- commands --------------------------------------------------------
    def show_palette(self, position, style) -> None:
        self.style_ = drawing.merge_style(style)
        raster = _rect_from(position)
        self.saved = raster
        if raster is None:
            screen = QGuiApplication.primaryScreen()
            raster = default_raster(self.style_, screen.geometry() if screen else QRect(0, 0, 1920, 1080))
        self._place(raster)
        self._apply_mode("locked" if self.saved is not None else "aligning")
        self.changed.emit("show")

    def _hide(self) -> None:
        self.mode = "hidden"
        self.hide()

    def hide_palette(self) -> None:
        self._hide()
        self.changed.emit("hide")

    def unlock(self) -> None:
        if self.mode != "locked":
            return
        self._apply_mode("aligning")
        self.changed.emit("unlock")

    def lock(self) -> None:
        """Keep the current position and lock. The user's lock: reason `lock`."""
        if self.mode != "aligning":
            return
        self.saved = self.raster()
        self._apply_mode("locked")
        self.changed.emit("lock")

    def cancel(self) -> None:
        """Esc: drop the change, back to the saved position, locked; hidden when
        there is none."""
        if self.mode != "aligning":
            return
        if self.saved is None:
            self._hide()
            self.changed.emit("cancel")
            return
        self._place(self.saved)
        self._apply_mode("locked")
        self.changed.emit("cancel")

    def set_style(self, style) -> None:
        raster = self.raster() if self.mode != "hidden" else None
        self.style_ = drawing.merge_style(style)
        if raster is not None:
            # The margin can grow with the font; the grid stays where it is.
            self._place(raster)
        self.update()
        self.changed.emit("style")

    # -- painting, mouse and keys -------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        drawing.draw_grid(painter, self.rect(), self.style_, self.mode == "aligning")
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self.mode != "aligning":
            return
        pos = event.position().toPoint()
        handle = QRect(self.width() - drawing.HANDLE, self.height() - drawing.HANDLE,
                       drawing.HANDLE, drawing.HANDLE).contains(pos)
        resize = handle or bool(event.modifiers() & Qt.ShiftModifier)
        self._drag = (resize, event.globalPosition().toPoint(), self.geometry())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._drag:
            return
        resize, start, geometry = self._drag
        delta = event.globalPosition().toPoint() - start
        if resize:
            m = self._margin()
            self.resize(max(geometry.width() + delta.x(), 2 * m + MIN_GRID[0]),
                        max(geometry.height() + delta.y(), 2 * m + MIN_GRID[1]))
        else:
            self.move(geometry.topLeft() + delta)

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self.lock()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self.mode != "aligning":
            return
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.lock()
            return
        if key == Qt.Key_Escape:
            self.cancel()
            return
        step = 10 if event.modifiers() & Qt.ControlModifier else 1
        delta = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0),
                 Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}.get(key)
        if not delta:
            return
        if event.modifiers() & Qt.ShiftModifier:
            m = self._margin()
            self.resize(max(self.width() + delta[0], 2 * m + MIN_GRID[0]),
                        max(self.height() + delta[1], 2 * m + MIN_GRID[1]))
        else:
            self.move(self.pos() + QPoint(*delta))


class RewardWindow(QWidget):
    """The reward overlay: a transparent window over the game window, drawing
    the recommendation. Never takes the mouse or the keys."""

    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("warframe-tracker reward overlay")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.X11BypassWindowManagerHint | Qt.WindowTransparentForInput
                            | Qt.WindowDoesNotAcceptFocus)
        self.shown_ = False
        self.token = None
        self.tiles: list = []
        self.best: int | None = None
        self.headline: str | None = None
        self.lines: list = []
        self.picked: int | None = None
        self.look: dict = drawing.merge_look(None)

    def show_rewards(self, command: dict) -> None:
        rect = _rect_from(command.get("window"))
        if rect is None:
            raise ValueError("reward_show without a window")
        tiles = []
        for tile in command.get("tiles") or []:
            x, y, w, h = (float(value) for value in tile)
            tiles.append((x, y, w, h))
        lines = [[str(line) for line in (card or [])][:3]
                 for card in (command.get("lines") or [])]
        self.tiles, self.lines = tiles, lines
        self.best = _index(command.get("best"), len(tiles))
        self.picked = _index(command.get("picked"), len(tiles))
        self.look = drawing.merge_look(command.get("look"))
        headline = command.get("headline")
        self.headline = str(headline) if headline else None
        self.token = command.get("token")
        self.setGeometry(rect)
        self.shown_ = True
        self.show()
        self.update()
        self.changed.emit("show")

    def pick(self, picked) -> None:
        if not self.shown_:
            return
        self.picked = _index(picked, len(self.tiles))
        self.update()
        self.changed.emit("pick")

    def hide_rewards(self) -> None:
        was = self.shown_
        self.shown_ = False
        self.hide()
        if was:
            self.changed.emit("hide")

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        drawing.draw_rewards(painter, self.rect(), self.tiles, self.best, self.headline, self.lines, self.picked,
                             self.look)
        painter.end()


def _index(value, count: int) -> int | None:
    """`value` as a card index inside `count` cards, or None."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if 0 <= index < count else None


class Controller(QObject):
    """Commands in, state out. Knows nothing about the socket: `send` is any
    function taking a dict, so the tests drive it without a process."""

    quit_requested = Signal()

    def __init__(self, window: PaletteWindow, send, rewards: RewardWindow | None = None):
        super().__init__()
        self.window = window
        self.send = send
        self._rewards = rewards
        window.changed.connect(self.report)
        if rewards is not None:
            rewards.changed.connect(self.report_rewards)

    @property
    def rewards(self) -> RewardWindow:
        """The reward overlay's window, made on first use."""
        if self._rewards is None:
            self._rewards = RewardWindow()
            self._rewards.changed.connect(self.report_rewards)
        return self._rewards

    def report_rewards(self, reason: str) -> None:
        window = self.rewards
        self.send({"type": "reward", "shown": window.shown_, "reason": reason,
                   "token": window.token, "picked": window.picked})

    def state(self, reason: str) -> dict:
        return {"type": "state", "shown": self.window.mode != "hidden",
                "mode": self.window.mode, "position": self.window.position(),
                "reason": reason}

    def report(self, reason: str) -> None:
        self.send(self.state(reason))

    def handle(self, command: dict) -> None:
        name = command.get("cmd") if isinstance(command, dict) else None
        try:
            if name == "palette_show":
                self.window.show_palette(command.get("position"), command.get("style"))
            elif name == "palette_hide":
                self.window.hide_palette()
            elif name == "palette_unlock":
                self.window.unlock()
            elif name == "palette_lock":
                self.window.lock()
            elif name == "palette_style":
                self.window.set_style(command.get("style"))
            elif name == "reward_show":
                self.rewards.show_rewards(command)
            elif name == "reward_pick":
                self.rewards.pick(command.get("picked"))
            elif name == "reward_hide":
                self.rewards.hide_rewards()
            elif name == "quit":
                self.quit_requested.emit()
            else:
                self.send({"type": "error", "message": f"unknown command {name!r}"})
        except Exception as exc:  # noqa: BLE001 - the tracker should hear of it
            self.send({"type": "error", "message": f"{name}: {type(exc).__name__}: {exc}"})


def lock_file(folder: str | None = None) -> QLockFile:
    """The per-user lock that keeps a second helper from starting."""
    folder = folder or QStandardPaths.writableLocation(QStandardPaths.TempLocation)
    return QLockFile(os.path.join(folder, f"wft-overlay-helper-{os.getuid()}.lock"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m ui.overlay.helper",
        description="Overlay helper of warframe-tracker. Started by the tracker, "
                    "which gives it the name of its local socket; not meant to be run by hand.")
    parser.add_argument("--socket", required=True, metavar="NAME",
                        help="name of the tracker's local socket")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    lock = lock_file()
    if not lock.tryLock(0):
        print("another overlay helper is already running", file=sys.stderr)
        return 3

    window = PaletteWindow()
    link = QLocalSocket()

    def send(message: dict) -> None:
        if link.state() == QLocalSocket.ConnectedState:
            link.write((json.dumps(message) + "\n").encode())
            link.flush()

    controller = Controller(window, send)
    controller.quit_requested.connect(app.quit)

    def read() -> None:
        while link.canReadLine():
            try:
                command = json.loads(bytes(link.readLine()).decode())
            except ValueError:
                send({"type": "error", "message": "unreadable line"})
                continue
            controller.handle(command)

    def failed(_error) -> None:
        if link.state() != QLocalSocket.ConnectedState:
            print(f"cannot reach the tracker: {link.errorString()}", file=sys.stderr)
            app.quit()

    link.readyRead.connect(read)
    link.connected.connect(lambda: controller.report("hello"))
    link.disconnected.connect(app.quit)
    link.errorOccurred.connect(failed)
    link.connectToServer(args.socket)
    # Ctrl+C and SIGTERM end it at once; Qt would hold them back until Python code runs.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
