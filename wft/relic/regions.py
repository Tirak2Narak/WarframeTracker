"""Where the reward tiles are.

All coordinates are fractions of the **game window**, never pixels and never
desktop coordinates. That single choice removes a whole class of problems the
previous attempt lived with.

How the previous attempt did it, for contrast: a rectangle was dragged over a full-screen
screenshot, and the result was stored as absolute pixels in virtual-desktop
space together with the total resolution to validate against
(`{x: 4800, y: 1700, width: 1500, height: 750}`, `totalBounds: 7680x2160`).
Every use then had to work out which monitor those coordinates fell on and
convert back to monitor-local space. Change a resolution, unplug a monitor, or
move the game, and the stored region is meaningless. On the target machine that
file was never even written — there were no real measurements, only the example
in the documentation.

Worse, the stored region covered **all four rewards as one band**, so the
individual rewards had to be separated afterwards out of the OCR text: guess how
many rewards there are by counting occurrences of "Prime", sort the fragments by
x, split at the largest gaps. Every step of that is a guess, and it fails on
Forma (no "Prime" in the name) or on any misreading of that one word.

Cropping each tile separately removes the question instead of answering it.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Measured on 379 live captures (3840x2160, one player; 338 are reward
#: screens). The tile top is the strongest row gradient of the mean screen
#: (0.2062 in the blue interface theme, 0.2069 in the light one: 1 px apart at
#: 4K), the bottom under the name bar 0.4255.
#:
#: Left and right edges are those of the coloured bar under each card, which is
#: exactly as wide as the card: 480 px of 3840, a gap of 5, 4, 5 px
#: between two bars (4.67 px on average), so 0.125 wide and 0.00122 apart. The
#: gap is not zero: with no gap between the tiles the third of three
#: cards sat 13 px and the fourth of four 15 px too far left at 4K.
#:
#: Measured at one interface scale: the game's UI scale setting was not varied,
#: and whether it moves the reward row is not known. A player whose names come
#: out wrong calibrates.
TILE_TOP = 0.2065
TILE_HEIGHT = 0.219
TILE_WIDTH = 0.125
TILE_GAP = 0.00122

#: The reward row is centred on the screen, and the tiles keep their size and
#: spacing whatever the squad size. Observed in play: with four rewards two
#: sit either side of the centre; with three, one is the centre with one
#: either side; with two, the outer pair is simply
#: absent. Measured: the middle of the bars is x 1920 of 3840 in all three
#: layouts (2, 3 and 4 cards), on 278, 34 and 11 screens.
#:
#: Expressed as centring rather than as a table of first-tile positions, so any
#: count is correct by construction. Between two tiles lies TILE_GAP; the pitch
#: is the width plus the gap.
ROW_CENTRE_X = 0.5

#: The name sits in the lower part of the tile. One-line names are anchored to
#: the *second* line rather than centred, so the crop always spans both lines -
#: fitting it to one line would clip every wrapped name.
#:
#: Word boxes of Tesseract (the words Prime, Blueprint and Forma, 98 of them):
#: the first line of a wrapped name starts at 0.381 to 0.388, a one-line name
#: (the second line) at 0.400 to 0.405, and the lowest descender ends at 0.429.
#: The band 0.380 to 0.430 holds both and none of the artwork above, where the
#: "Owned" and "Crafted" badges sit (they were once read as characters).
#: Read with these through the hotkey's path on all 347 screens, against every
#: prime part: 1148 of 1242 tiles named at 3840x2160 and 1143 at 1920x1080,
#: against 1154 at 3840x2160 for the hand-calibrated regions of the same player.
NAME_TOP = 0.380
NAME_HEIGHT = 0.050

#: The "Owned" pill, top left, overlapping the tile edge slightly. Present only
#: on parts held in the inventory - which is not the same as "already built".
OWNED_BADGE = (-0.02, -0.01, 0.40, 0.10)


@dataclass(frozen=True)
class Region:
    """A rectangle as fractions of the window: x, y, width, height."""

    x: float
    y: float
    width: float
    height: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)

    def inset(self, other: "Region") -> "Region":
        """Interpret `other` as fractions *of this region*, returning window fractions."""
        return Region(
            self.x + other.x * self.width,
            self.y + other.y * self.height,
            other.width * self.width,
            other.height * self.height,
        )


def pitch() -> float:
    """From the left edge of one tile to the left edge of the next."""
    return TILE_WIDTH + TILE_GAP


def row_span(reward_count: int) -> float:
    """Width of the whole row: the tiles and the gaps between them."""
    return reward_count * TILE_WIDTH + (reward_count - 1) * TILE_GAP


def first_tile_x(reward_count: int) -> float:
    """Left edge of the leftmost tile, from the row being centred."""
    return ROW_CENTRE_X - row_span(reward_count) / 2


def tile(index: int, reward_count: int = 4) -> Region:
    """The whole tile at `index` (0-based, left to right as shown in game)."""
    return Region(first_tile_x(reward_count) + index * pitch(),
                  TILE_TOP, TILE_WIDTH, TILE_HEIGHT)


def name_region(index: int, reward_count: int = 4) -> Region:
    """Just the name text of a tile — no artwork, no player names, no badge.

    Excluding the artwork is not tidiness. Every OCR failure observed so far
    came from something non-textual inside the crop: a tick icon turned "2" into
    "@2". The earlier attempts tried to repair that afterwards with blocklists
    of UI words; not including it is cheaper and complete.
    """
    return Region(tile(index, reward_count).x, NAME_TOP, TILE_WIDTH, NAME_HEIGHT)


def owned_badge_region(index: int, reward_count: int = 4) -> Region:
    return tile(index, reward_count).inset(Region(*OWNED_BADGE))


#: The built-in values as measured, kept apart from the module constants: the
#: constants are what a config reading puts in force, these are what it starts
#: from and what a calibration is compared with.
_BUILTIN = {
    "tile_top": TILE_TOP, "tile_height": TILE_HEIGHT, "tile_width": TILE_WIDTH,
    "tile_gap": TILE_GAP, "row_centre_x": ROW_CENTRE_X,
    "name_top": NAME_TOP, "name_height": NAME_HEIGHT,
}

#: The keys `[regions.reward]` understands, in the order they are shown.
#: `tile_pitch` is the older spelling of the gap (see `load_overrides`).
REWARD_KEYS = ("tile_top", "tile_height", "tile_width", "tile_pitch", "tile_gap",
               "row_centre_x", "name_top", "name_height")


def load_overrides(cfg) -> None:
    """Put the built-in values in force, then lay the config's overrides on them.

    Kept as module-level constants that config can replace, so the common case
    needs no configuration at all and an unusual setup needs no code change.
    It starts from the built-in values on every call, not from whatever the
    constants hold: a key taken out of the config must bring the built-in value
    back at the next reading, not at the next start.

    `tile_gap` is the space between two tiles. An older file has `tile_pitch`
    instead (what the first calibration wrote, always equal to the width): with
    no `tile_gap` beside it the gap is worked out from it, and a file with only
    `tile_width` keeps the built-in gap.
    """
    global TILE_TOP, TILE_HEIGHT, TILE_WIDTH, TILE_GAP, NAME_TOP, NAME_HEIGHT
    global ROW_CENTRE_X

    base = _BUILTIN
    section = cfg.get("regions.reward", {}) or {}
    TILE_TOP = float(section.get("tile_top", base["tile_top"]))
    TILE_HEIGHT = float(section.get("tile_height", base["tile_height"]))
    TILE_WIDTH = float(section.get("tile_width", base["tile_width"]))
    if "tile_gap" in section:
        TILE_GAP = float(section["tile_gap"])
    elif "tile_pitch" in section:
        TILE_GAP = max(0.0, float(section["tile_pitch"]) - TILE_WIDTH)
    else:
        TILE_GAP = base["tile_gap"]
    NAME_TOP = float(section.get("name_top", base["name_top"]))
    NAME_HEIGHT = float(section.get("name_height", base["name_height"]))
    ROW_CENTRE_X = float(section.get("row_centre_x", base["row_centre_x"]))


def configured_overrides(cfg) -> list[tuple[str, float, float]]:
    """What the config sets in `[regions.reward]`: (key, configured, built-in).

    The one answer the calibration dialog, Settings and `wf doctor` all give,
    so they cannot disagree. Empty when the built-in values are in force. For
    `tile_pitch` the built-in counterpart is the width plus the gap.
    """
    section = cfg.get("regions.reward", {}) or {}
    found = []
    for key in REWARD_KEYS:
        if key not in section:
            continue
        builtin = (_BUILTIN["tile_width"] + _BUILTIN["tile_gap"]
                   if key == "tile_pitch" else _BUILTIN[key])
        try:
            value = float(section[key])
        except (TypeError, ValueError):
            continue
        found.append((key, value, builtin))
    return found
