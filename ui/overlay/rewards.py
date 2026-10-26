"""The reward overlay on the tracker's side.

After a reading the recommendation is drawn over the game's reward screen by the
overlay helper (`ui.overlay.helper`). This module decides *what* is sent and
*how long* it stands:

* `show_command` builds the `reward_show` command from what the Rewards page
  already knows (the cards, the recommended one, the lines under each card).
* `Lifetime` is the rule for ending it: with the reward screen, not by a timer
  alone. Every half second the strip of the game window that holds the header's
  yellow word is grabbed and compared with the header of the reading
  (`wft.relic.header`); two checks in a row without the header end the overlay,
  and 15 seconds after the reading end it whatever happens (the screen is never
  up longer).
* `RewardOverlay` ties both to the helper's client and reports whether the
  helper really showed it, so the desktop notification can be left out
  only when it did.

* The card the player clicks: while the overlay stands and the switch
  `[overlay] click_to_pick` is on, `Arming` is set and the click worker
  (`ui.workers.pick_click`) reports left clicks in the game window. `card_at`
  turns the pointer position into a card, the last click counts, the tick moves
  to it, and when the overlay ends the open choice goes out as `settling` so
  the Rewards page books it through the path of its "I took this" button.

It reads the game window's header strip and the pointer position, and sends no
input.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from ui.overlay import drawing, looks
from wft.relic import header

#: How often the header is looked at while the overlay stands.
POLL_MS = 500

#: Longest the overlay stands, counted from the reading. The reward screen is up
#: for 15 seconds at most.
MAX_SECONDS = 15.0

#: Checks in a row without the header that end the overlay.
MISSES = 2

#: How long the test button's overlay stands, with no header comparison.
TEST_SECONDS = 5.0

#: How long the helper may take to confirm before the reading counts as not
#: shown (and the notification goes out). A helper that has to start needs a
#: moment; one that is running confirms at once.
CONFIRM_MS = 3000


def show_command(rect: dict, tiles: list, best: int | None, headline: str | None,
                 lines: list, token: int, picked: int | None = None,
                 look: dict | None = None) -> dict:
    """The `reward_show` command (protocol in `ui.overlay.helper`). `look` is
    `{"frame", "text"}` as "#rrggbb", read from the configuration at every show."""
    return {"cmd": "reward_show", "window": dict(rect),
            "tiles": [list(tile) for tile in tiles], "best": best,
            "headline": headline, "lines": [list(card) for card in lines],
            "picked": picked, "token": token,
            "look": drawing.merge_look(look)}


def card_at(x: float, y: float, placement, tiles) -> int | None:
    """The card under the point (x, y) in root-window coordinates, or None.

    `placement` has `x`, `y`, `width`, `height` (the game window on the desktop,
    `wft.capture.window.Placement`); `tiles` are `(x, y, w, h)` as fractions of
    that window. A point between two cards or outside all of them gives None.
    """
    if not placement.width or not placement.height:
        return None
    fx = (x - placement.x) / placement.width
    fy = (y - placement.y) / placement.height
    for index, (tx, ty, tw, th) in enumerate(tiles):
        if tx <= fx < tx + tw and ty <= fy < ty + th:
            return index
    return None


class Arming:
    """Whether clicks are read right now: a flag that two threads share.

    The GUI thread sets and clears it, the click worker waits on it. Every
    change also writes a byte into a pipe the worker selects on, so the worker
    sleeps without a timeout and still notices at once (no polling).
    """

    def __init__(self):
        self._event = threading.Event()
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        os.set_blocking(self._write_fd, False)

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        if not self._event.is_set():
            self._event.set()
            self.wake()

    def clear(self) -> None:
        if self._event.is_set():
            self._event.clear()
            self.wake()

    def wake(self) -> None:
        try:
            os.write(self._write_fd, b"x")
        except OSError:
            pass

    def fileno(self) -> int:
        return self._read_fd

    def drain(self) -> None:
        try:
            while os.read(self._read_fd, 4096):
                pass
        except OSError:
            pass

    def __del__(self):
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class Lifetime:
    """When the overlay ends. Pure: the caller brings the time and the answer of
    the header check, so the tests drive it with a fake clock."""

    def __init__(self, started: float, max_seconds: float = MAX_SECONDS,
                 misses: int = MISSES):
        self.started = started
        self.max_seconds = max_seconds
        self.misses = misses
        self._missed = 0
        #: Why `keep` last said no: `time` or `header`.
        self.reason = ""
        #: The time of the first check that did not find the header, once per
        #: overlay (kept when the header comes back), and whether the header
        #: was compared at all.
        self.first_miss: float | None = None
        self.compared = False

    @property
    def missing(self) -> bool:
        """The last check did not find the header."""
        return self._missed > 0

    def keep(self, present: bool | None, now: float) -> bool:
        """One check. `present` is whether the header is there, or None when it
        is not compared (the picture of the reading had none, or the test
        button). False means: end the overlay now."""
        if now - self.started >= self.max_seconds:
            self.reason = "time"
            return False
        if present is None:
            return True
        self.compared = True
        if not present and self.first_miss is None:
            self.first_miss = now
        self._missed = 0 if present else self._missed + 1
        if self._missed >= self.misses:
            self.reason = "header"
            return False
        return True


class RewardOverlay(QObject):
    #: The helper showed the overlay of this token.
    shown = Signal(int)
    #: The overlay of this token could not be shown (the helper failed, stopped
    #: or did not answer in time).
    unavailable = Signal(int)
    #: The overlay ended: `reason` is `header` (the screen is gone), `time`,
    #: `replaced` or `hidden`.
    ended = Signal(str)
    #: The overlay of this token (first) ends with this card (second) as the
    #: player's last click and nothing booked yet: book it now. Not sent
    #: for the test button's overlay.
    settling = Signal(int, int)
    #: One short line about a click (`click: card 3`, `click: outside the
    #: cards`) or about the overlay's life (`overlay: ended, header gone ...`),
    #: for the Rewards page's log. No coordinates.
    note = Signal(str)

    def __init__(self, cfg, client, grab: Callable | None = None,
                 clock: Callable[[], float] = time.time, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.client = client
        self._grab = grab or header.grab_strip
        self._clock = clock
        self._token = 0
        self._pending: int | None = None
        #: A card clicked while the header was missing: counts only if the
        #: header comes back.
        self._held: int | None = None
        self._life: Lifetime | None = None
        self._mask = None
        self._where = None
        self._tiles: list = []
        #: The card the player clicked last, for the running overlay.
        self.picked: int | None = None
        #: The running overlay is the test button's: ticks, never books.
        self._test = False
        #: Shared with the click worker: set only while clicks are to be read.
        self.arming = Arming()
        self.last_error = ""
        self.confirm_ms = CONFIRM_MS
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self.tick)
        self._confirm = QTimer(self)
        self._confirm.setSingleShot(True)
        self._confirm.timeout.connect(self._confirm_timed_out)
        client.reward_state.connect(self._on_state)
        client.failed.connect(self._on_failed)
        client.stopped.connect(self._on_stopped)

    def current_placement(self):
        """Where the game window is now (`wft.capture.window.Placement`), or None
        when there is none or X cannot be asked. Tests replace `finder`."""
        try:
            return self.finder()
        except Exception:  # noqa: BLE001 - no window, no X: nothing to lie over
            return None

    def finder(self):
        from wft.capture import window as cap

        window_class = str(self.cfg.get("game.window_class", ""))
        game = cap.find_game(window_class) if window_class else None
        return cap.placement(game) if game is not None else None

    # -- switches ------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """The switch on the Overlays page (`[overlay] rewards`)."""
        return bool(self.cfg.get("overlay.rewards", True))

    @property
    def click_enabled(self) -> bool:
        """The switch on the Overlays page (`[overlay] click_to_pick`)."""
        return bool(self.cfg.get("overlay.click_to_pick", True))

    def refresh_arming(self) -> None:
        """Clicks are read only while the overlay stands (confirmed by the
        helper, not yet ended) and the switch is on."""
        if self._life is not None and self._pending is None and self.click_enabled:
            self.arming.set()
        else:
            self.arming.clear()

    @property
    def active(self) -> bool:
        return self._life is not None

    # -- showing ---------------------------------------------------------------
    def show(self, command_args: dict, placement, mask, started: float | None = None,
             seconds: float = MAX_SECONDS, test: bool = False) -> int:
        """Send the overlay; the token comes back with `shown` or `unavailable`.

        `command_args` are the keyword arguments of `show_command` except the
        rectangle and the token. `mask` is the header mask of the reading's
        picture (None: no header comparison, the time alone). A running overlay
        is replaced. `test` is the test button: clicks tick a card but nothing is
        ever booked.
        """
        if self._life is not None:
            self._end("replaced", hide=False)
        self._token += 1
        token = self._token
        self._pending = token
        self._where = placement
        self._mask = mask
        self._tiles = [tuple(tile) for tile in command_args.get("tiles", [])]
        #: A reading of the same screen as the one before keeps its tick.
        self.picked = command_args.get("picked")
        self._test = test
        now = self._clock()
        self._life = Lifetime(now if started is None else started, seconds)
        self.last_error = ""
        self.client.send(show_command(placement.as_rect(), token=token,
                                      look=looks.look_from_config(self.cfg), **command_args))
        self._confirm.start(self.confirm_ms)
        return token

    def pick(self, index: int | None) -> None:
        """Put the tick on a card."""
        if self._life is not None:
            self.picked = index
            self.client.send({"cmd": "reward_pick", "picked": index})

    def on_click(self, x: int, y: int) -> None:
        """A left click at (x, y) in the game window (from the click worker).
        The last click on a card counts; a click elsewhere changes nothing."""
        if self._life is None or self._pending is not None or self._where is None:
            return
        index = card_at(x, y, self._where, self._tiles)
        if index is not None and self._life.missing:
            # The screen may already be closed and this click belongs to the
            # next one: hold it until the next check.
            self._held = index
            self.note.emit("click: after the screen closed")
        elif index is not None:
            self.note.emit(f"click: card {index + 1}")
            self.pick(index)
        else:
            self.note.emit("click: outside the cards")

    def take_over_pick(self) -> int | None:
        """The card ticked so far, handed on without booking it and cleared:
        the Rewards page calls this when a new reading is the same screen as the
        one before, so the choice is booked once, at the end."""
        index, self.picked = self.picked, None
        return index

    def settle_now(self) -> None:
        """Hand the open choice on for booking, as if the overlay ended (the
        Rewards page calls this before it takes a new reading in)."""
        if self._life is not None:
            self._settle()

    def _settle(self) -> None:
        index, self.picked = self.picked, None
        if index is not None and not self._test:
            self.settling.emit(self._token, index)

    def hide(self) -> None:
        if self._life is not None:
            self._end("hidden", hide=True)

    # -- the lifetime ------------------------------------------------------------
    def tick(self) -> None:
        """One check of the header; ends the overlay when it is over."""
        if self._life is None or self._pending is not None:
            # Not before the helper confirmed: the 15 seconds run, the header
            # check starts with the overlay.
            if self._life is not None and not self._life.keep(None, self._clock()):
                self._end(self._life.reason, hide=True)
            return
        present = None
        if self._mask is not None and self._where is not None:
            try:
                present = header.is_present(self._mask, self._grab(self._where))
            except Exception:  # noqa: BLE001 - a window that cannot be read is gone
                present = False
        now = self._clock()
        first = self._life.first_miss
        keep = self._life.keep(present, now)
        if present and keep and self._held is not None:
            # One missed frame, the header is back: the held click was real.
            held, self._held = self._held, None
            self.note.emit(f"click: card {held + 1}")
            self.pick(held)
        if first is None and self._life.first_miss is not None:
            self.note.emit(
                f"overlay: header not seen for the first time, "
                f"{self._life.first_miss - self._life.started:.1f} s after the reading")
        if not keep:
            self._end(self._life.reason, hide=True)

    def end_line(self, reason: str) -> str:
        """The log line for the end of the running overlay: why, and when."""
        life, now = self._life, self._clock()
        since = f"{now - life.started:.1f} s after the reading"
        if reason == "header":
            gone = now - life.first_miss if life.first_miss is not None else 0.0
            return (f"overlay: ended, header gone {gone:.1f} s after the first "
                    f"miss ({since})")
        if reason == "time":
            if self._pending is not None:
                detail = "the helper had not confirmed"
            elif not life.compared:
                detail = "no header check"
            elif life.first_miss is None:
                detail = "header still seen"
            else:
                detail = (f"header missed at "
                          f"{life.first_miss - life.started:.1f} s, not twice in a row")
            return f"overlay: ended at the {life.max_seconds:g} s limit ({detail})"
        if reason == "replaced":
            return f"overlay: replaced by a new reading ({since})"
        if reason == "hidden":
            return f"overlay: hidden ({since})"
        return f"overlay: ended, {reason} ({since})"

    def _end(self, reason: str, hide: bool) -> None:
        self._poll.stop()
        self._confirm.stop()
        if self._life is not None:
            self.note.emit(self.end_line(reason))
            self._settle()
        self._pending = None
        self._held = None
        self._life, self._mask, self._where = None, None, None
        self._tiles, self.picked = [], None
        self.arming.clear()
        if hide:
            self.client.send({"cmd": "reward_hide"})
        self.ended.emit(reason)

    # -- what the helper says -----------------------------------------------------
    def _on_state(self, message: dict) -> None:
        if (message.get("shown") and message.get("reason") == "show"
                and message.get("token") == self._pending):
            token, self._pending = self._pending, None
            self._confirm.stop()
            if self._life is not None:
                self._poll.start()
                self.refresh_arming()
            self.shown.emit(token)

    def _on_failed(self, message: str) -> None:
        self.last_error = message
        self._give_up()

    def _on_stopped(self) -> None:
        self._give_up()

    def _confirm_timed_out(self) -> None:
        self.last_error = "The overlay helper did not confirm in time."
        self._give_up(hide=True)

    def _give_up(self, hide: bool = False) -> None:
        """The pending overlay will not be shown (or the helper is gone)."""
        token = self._pending
        if self._life is not None:
            self._end("failed", hide=hide)
        if token is not None:
            self.unavailable.emit(token)
