"""The last reward screen the hotkey read, kept in memory for whoever needs it.

Three places want the same thing: the Rewards page (was this read well enough,
or should the regions be calibrated on it), the Setup page (has a reward screen
been read yet) and the calibration dialog (a picture of a real reward screen,
without a second tool and without racing the fifteen seconds the screen is
up). They all read it from here, so what "read badly" means is decided once.

**Memory only.** The picture is the whole game window and can show the names of
the other players in the squad; nothing in this module writes it anywhere. (The
opt-in `capture.keep_rewards` is a separate, explicit thing in
`ui/workers/relic_trigger.py`.) It ends with the process.

What counts as a bad reading is not a new threshold. It is the case the Rewards
page already treats as a failure: the reading raised an error ("RESCAN"), or
not one tile was recognised ("nothing recognised", the "None of them read"
line). A partly recognised screen is the normal case — a squadmate's part the
log does not narrow, a tile caught mid-animation — and says nothing about the
regions.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from wft.relic import score as S


@dataclass(frozen=True)
class Reading:
    """What a reading of a reward screen came to."""

    recognised: int      # tiles read as a part, or as Forma (which is no part)
    total: int           # tiles laid out
    failed: bool         # an error, i.e. the RESCAN case

    @property
    def bad(self) -> bool:
        """Failed outright, or not one tile recognised."""
        return self.failed or self.recognised == 0


def reading_of(result) -> Reading:
    """Summarise a `TriggerResult`. The one place that counts recognised tiles.

    Forma counts as recognised: it is a real reward that belongs to no prime
    set and so never resolves to a part, and `read_at` counts it as read for the
    same reason.
    """
    if result.error or result.verdict is None:
        return Reading(0, result.reward_count or 0, True)
    rewards = result.verdict.rewards
    recognised = sum(1 for r in rewards
                     if r.part_unique is not None or S.looks_like_forma(r.raw_text))
    return Reading(recognised, len(rewards), False)


@dataclass(frozen=True)
class LastScreen:
    image: object        # BGR numpy array of the whole game window, read-only
    taken_at: float      # time.time()
    reading: Reading
    #: The `TriggerResult` this picture was read into (None when not given):
    #: the overlay's test button shows the last reading again from it.
    result: object = None

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    def age_seconds(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.taken_at)


_lock = threading.Lock()
_current: LastScreen | None = None


def remember(image, result, now: float | None = None) -> LastScreen:
    """Keep this picture and its reading as the last reward screen.

    Called from the reading thread, once per hotkey press. The array is marked
    read-only so no reader can alter what another one is about to show.
    """
    global _current
    image.flags.writeable = False
    screen = LastScreen(image, time.time() if now is None else now,
                        reading_of(result), result)
    with _lock:
        _current = screen
    return screen


def current() -> LastScreen | None:
    with _lock:
        return _current


def forget() -> None:
    global _current
    with _lock:
        _current = None


def age_words(seconds: float) -> str:
    """"just now", "4 min ago", "3 h ago": the age as the buttons say it."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"
