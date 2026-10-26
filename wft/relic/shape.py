"""Does a loaded screenshot have the shape of the game window?

Every reward region is a fraction of the *game window*. A picture that shows
more than the game — the whole desktop, a second monitor beside it, a window
frame — puts those fractions on the wrong pixels, and nothing downstream can
tell: the dialog marks a box, the numbers look plausible, and the hotkey then
reads the wrong place for good. The shape of the picture is the one thing
checkable without looking at it.

**Tolerance, 1 %.** Rows added on top of the game (a title bar) move every
region down by their share of the height, and the name band is only about 0.05
of the window high. Measured on 39 live reward screens at 3840x2160 with the
shipped regions, padding on top by 0.5 / 1 / 2 / 3 / 5 % of the height left
91 / 93 / 81 / 3 / 3 % of the names that read at 0 % unchanged: from 3 %
on nothing reads, and at 1 % the loss (7 points) is a few above what a change
that does not move the rows costs (2 to 6 points with bars added at the sides).
Screenshots of the game itself stay far inside 1 %: 1920x1080 and
3840x2160 are the same 16:9 to the last digit, 1366x768 is 0.05 % off it and
1360x768 0.4 %. A window with a 30 px title bar (1920x1110) is 2.7 % off. Two
1080p monitors side by side (3840x1080) are 100 % off.

Without a running game there is nothing to compare with, so the picture is held
against the shapes a game window really has. 32:9 is not among them: it is
indistinguishable from two 16:9 monitors side by side (3840x1080 is both), and
the second is the mistake being caught. A 32:9 player with the game running is
compared with the window and is not warned.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Relative difference of the aspect ratio that is still "the same shape".
TOLERANCE = 0.01

#: Aspect ratios of a game window. "21:9" is sold as 2560x1080 (2.370),
#: 3440x1440 (2.389) and written 7:3 (2.333), so its entry sits in the middle
#: and is given the width to cover all three.
USUAL = (
    ("16:9", 16 / 9, TOLERANCE),
    ("16:10", 16 / 10, TOLERANCE),
    ("4:3", 4 / 3, TOLERANCE),
    ("5:4", 5 / 4, TOLERANCE),
    ("21:9", 2.36, 0.025),
)


@dataclass(frozen=True)
class Mismatch:
    """A picture that does not have the shape of the game window."""

    picture: tuple[int, int]
    #: "the game window" when compared with a running game, else "a game window".
    against_window: bool
    window: tuple[int, int] | None


def ratio(size: tuple[int, int]) -> float:
    return size[0] / size[1]


def check(picture: tuple[int, int], window: tuple[int, int] | None = None) -> Mismatch | None:
    """None when the picture can be a screenshot of the game window.

    `picture` and `window` are (width, height). With a window, the picture must
    have its shape; without, one of the usual shapes.
    """
    shape = ratio(picture)
    if window is not None:
        if abs(shape / ratio(window) - 1) <= TOLERANCE:
            return None
        return Mismatch(picture, True, window)
    for _name, usual, tolerance in USUAL:
        if abs(shape / usual - 1) <= tolerance:
            return None
    return Mismatch(picture, False, None)


def describe(size: tuple[int, int]) -> str:
    """"1920x1080 (16:9)": the size and the nearest usual shape, or just the size."""
    shape = ratio(size)
    nearest = min(USUAL, key=lambda u: abs(shape / u[1] - 1))
    name = nearest[0] if abs(shape / nearest[1] - 1) <= nearest[2] else f"{shape:.2f}:1"
    return f"{size[0]}x{size[1]} ({name})"
