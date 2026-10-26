"""Reading the stack count off a tile.

Quantity and rank are not two readings to tell apart. They are two different
places on the tile, and knowing that removes the whole problem:

    quantity   badge at the top left, tick icon then digits
    rank       inline in the name text, in square brackets: "Gauss Prime [30]"

Measured positions, as fractions of the tile:

    tick icon      x 0.03 - 0.09,  y 0.05 - 0.11
    digits         immediately right of the icon
    name (1 line)  y 0.82 - 0.95
    name (wrapped) y 0.75 - 0.98

Two things were measured and both matter. Including the tick icon in the crop
made Tesseract return "@2" instead of "2" — the icon is read as a character. And
a badge is present **only on stackable items**: Warframes and weapons do not
stack, so two of them are two tiles. Inventory count is therefore tiles times
badge quantity, never a count of distinct names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

#: The badge area, as fractions of the tile. Starts right of the tick icon:
#: including the icon costs a wrong reading, and the icon carries no
#: information the digits do not.
BADGE_REGION = (0.085, 0.03, 0.28, 0.11)   # x, y, width, height

#: Digits only. Warframe writes large counts with a separator ("2,515"), so the
#: comma is allowed and stripped afterwards.
DIGIT_ALLOWLIST = "0123456789,."

#: Below this the badge is treated as absent rather than misread. A tile without
#: a badge is the normal case, so a confident "no badge" matters more than
#: squeezing out an uncertain number.
MIN_CONFIDENCE = 60.0

_RANK = re.compile(r"\[\s*(\d+)\s*\]")
_DIGITS = re.compile(r"\d[\d,.]*")


@dataclass
class Quantity:
    count: int | None          # None means no badge, which is not the same as 1
    confidence: float
    raw: str


def strip_rank(name: str) -> tuple[str, int | None]:
    """Split "Gauss Prime [30]" into ("Gauss Prime", 30).

    The rank is worth keeping: it says whether an owned item is already levelled,
    which feeds mastery. It must never be read as a quantity — that would turn
    one rank-30 weapon into thirty of them.
    """
    match = _RANK.search(name)
    if not match:
        return name.strip(), None
    rank = int(match.group(1))
    return _RANK.sub("", name).strip(), rank


#: Tolerant of OCR losing the brackets, which it does about as often as it
#: loses them around a rank. The word itself is long and distinctive enough
#: that a bare trailing "Radiant" is not going to be anything else.
_REFINEMENT = re.compile(
    r"[\[\(]?\s*(Intact|Exceptional|Flawless|Radiant)\s*[\]\)]?\s*$",
    re.IGNORECASE)


def strip_refinement(name: str) -> tuple[str, str | None]:
    """Split "Meso L3 Relic [Radiant]" into ("Meso L3 Relic", "RADIANT").

    A relic's refinement is printed on its tile, and it is not decoration: it
    is worth a factor of five on a rare drop. Two refinements of the same relic
    are two tiles with two counts, so reading the name without it both loses
    the odds and makes the second tile look like a duplicate of the first.
    """
    match = _REFINEMENT.search(name)
    if not match:
        return name.strip(), None
    return _REFINEMENT.sub("", name).strip(), match.group(1).upper()


#: The four levels, for the abbreviated form below.
_REFINEMENT_WORDS = ("INTACT", "EXCEPTIONAL", "FLAWLESS", "RADIANT")

#: Shortest abbreviation accepted for a refinement. Measured against the whole
#: catalogue: no tracked name - 4073 of them - ends in a word that begins any
#: of the four, even at two letters. So three is not the shortest that is safe,
#: it is a margin over the shortest that is safe, kept because the measurement
#: describes today's catalogue and a two-letter tail is far likelier to be a
#: half-typed word than an intended "Radiant".
_REFINEMENT_ABBREVIATION = 3


def split_typed_refinement(text: str) -> tuple[str, str | None]:
    """Split a refinement off text a person typed, abbreviations allowed.

    Deliberately not `strip_refinement`, and the two must not be merged. That
    one reads OCR, where a tail resembling "radi" is more likely damage than a
    word, and the scan's answers are pinned byte for byte by the fixture test.
    This one reads a human who has been asked to name a relic and does not
    want to type "[Radiant]" in full: "neo c9 radi" is the case that
    came up when the brackets had to be written by hand.

    Only the last word is considered, and only as a prefix of one of the four
    levels - so "Neo C9 Relic radi" splits and "Radial Javelin" does not.
    """
    stripped = (text or "").strip()
    words = stripped.split()
    if len(words) < 2:
        # A refinement alone names no relic, so there is nothing to split off.
        return stripped, None
    tail = words[-1].strip("[]()").casefold()
    if len(tail) < _REFINEMENT_ABBREVIATION:
        return stripped, None
    for level in _REFINEMENT_WORDS:
        if level.casefold().startswith(tail):
            return " ".join(words[:-1]).strip(), level
    return stripped, None


def _prepare(crop: np.ndarray) -> np.ndarray:
    """Threshold and enlarge a badge crop.

    Unlike the reward screen, where raw crops read best, the badge is small and
    sits on the tile artwork. Otsu plus a threefold enlargement was measured to
    turn an unreadable crop into a correct one.
    """
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Text is bright on dark; Tesseract expects the opposite.
    if binary.mean() < 127:
        binary = 255 - binary
    return cv2.resize(binary, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)


#: The badge is drawn with a bright tick icon in a circle, in the same accent
#: colour as the count beside it. Looking for that colour is a few hundred
#: microseconds; running Tesseract is a process start, about two thirds of a
#: second. Most tiles have no badge at all, so asking the cheap question first
#: is the difference between a scan that reads stack counts in seconds and one
#: that takes ten minutes over them.
#:
#: Deliberately generous: this only has to be certain about *absence*. Anything
#: that might be a badge goes to the reader, which then decides properly.
ICON_REGION = (0.0, 0.0, 0.42, 0.16)     # x, y, width, height
MIN_ICON_PIXELS = 0.002


def has_badge(tile_crop: np.ndarray) -> bool:
    """Whether this tile shows a stack badge at all.

    Saturation and brightness rather than a specific hue: the interface has
    themes, and the badge follows the theme. What stays true is that the icon is
    a bright saturated mark against tile artwork, which is neither.
    """
    height, width = tile_crop.shape[:2]
    x = int(ICON_REGION[0] * width)
    y = int(ICON_REGION[1] * height)
    box_w = int(ICON_REGION[2] * width)
    box_h = int(ICON_REGION[3] * height)
    if box_w < 4 or box_h < 4:
        return True                      # too small to judge; let the reader look

    corner = tile_crop[y:y + box_h, x:x + box_w]
    if corner.size == 0:
        return True
    hsv = cv2.cvtColor(corner, cv2.COLOR_BGR2HSV)
    bright = ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 110))
    if float(bright.mean()) >= MIN_ICON_PIXELS:
        return True

    # Second chance on plain contrast, for a badge whose colour the first test
    # does not expect. An empty corner is flat; anything drawn on it is not.
    grey = cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY)
    return float(grey.std()) > 18.0


#: The tick is a ring, so it is not a filled disc - measured fill is about
#: 0.38 - but it is very nearly square. Tile artwork that survives thresholding
#: is not: the false badges measured on real captures were thin vertical
#: strokes from the frame around the artwork (aspect 0.07 and 0.15, read as
#: "11" and "111") and horizontal smears (2.59, read as "7"). Every real badge
#: measured between 0.93 and 1.15.
TICK_ASPECT = (0.7, 1.4)

#: The tick is as tall as the digits beside it, so anything much shorter than
#: the crop is a fragment rather than the icon.
MIN_TICK_HEIGHT = 0.3

#: How much of its own bounding box the tick fills. This is the measurement
#: the comment above always described and nothing checked: the icon is a ring,
#: and a ring is mostly hole.
#:
#: Measured over the fixtures, every one of the fifteen real badges lands
#: between 0.37 and 0.39 - it is one icon at one size, so the spread is tiny.
#: A blueprint tile has none: Warframe draws no badge for a stack of one, and
#: on "Gunsen Prime Blueprint" a black frond of the artwork sat in the corner,
#: tall and square enough to pass both existing tests, at 0.74. The blueprint
#: grid behind it was then read as its digits and a single blueprint was
#: recorded as seven.
#:
#: 0.45 rather than 0.5: two more fixtures that hold no badge measure 0.46 and
#: 0.49, and this is the first gate rather than the only one - the cheaper it
#: is to refuse here, the less has to be caught later.
MAX_TICK_FILL = 0.45

#: And the floor of that band. A ring is not an outline either: Warframe draws
#: some markers in a square frame (the one on "Afentis Prime Handle", the
#: infinity sign), and a frame is square and hollow enough to pass every test
#: above - at 0.10 and 0.09. The symbol inside it was then read as the
#: figures, and a single handle was recorded as 494. Every real tick measures
#: 0.37 - 0.39, at full resolution and in the 320px copies alike.
MIN_TICK_FILL = 0.25

#: How far into the badge crop the tick may start, as a fraction of its
#: width. The count is drawn to the right of the tick, so the tick is at the
#: left: measured over every badge fixture, its left edge lies between 0.08
#: and 0.24. On "Gunsen Prime Blueprint", which holds no badge, a piece of the
#: gun's artwork at 0.78 - cut off by the crop's right edge - passed as the
#: tick, and the piece beside it was read as a 4.
MAX_TICK_LEFT = 0.40


def _blobs(prepared: np.ndarray) -> list[tuple[int, int, int, int, float]]:
    """(left, right, height, middle, fill) of every mark, left to right.

    `fill` is how much of its own bounding box the mark's ink covers, which is
    what tells a ring from a blot.

    The floor is relative to the crop, not an absolute pixel count: the same
    tile is cropped at two resolutions in this codebase - the frame on disk
    and the smaller copy kept for the review - and a fixed threshold quietly
    means something different in each.
    """
    ink = (prepared < 128).astype(np.uint8)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    height = ink.shape[0]
    floor = max(12, int(height * height * 0.004))
    return sorted((stats[i][0], stats[i][0] + stats[i][2], stats[i][3],
                   stats[i][1] + stats[i][3] // 2,
                   stats[i][4] / max(1, stats[i][2] * stats[i][3]))
                  for i in range(1, count) if stats[i][4] >= floor)


def _tick(blobs, crop_height: int, crop_width: int) -> int | None:
    """Index of the tick icon among `blobs`, or None.

    The leftmost mark that is tall enough and square. Leftmost because the
    count is only ever drawn to the right of the icon; square because that is
    what separates the icon from both the figures beside it and the artwork
    that bleeds in - measured false badges were vertical strokes at aspect
    0.07 and 0.15 and horizontal smears at 2.59, while every real icon
    measured between 0.93 and 1.15.

    One rule, used both to decide whether a badge is there and to find where
    it ends. They were two rules with two thresholds, 0.30 and 0.45, and a
    taller crop landed between them: the first said "badge", the second could
    not find it, and the icon was read as a figure - a stack of 2 came back
    as 92.
    """
    floor = crop_height * MIN_TICK_HEIGHT
    for index, (left, right, height, _middle, fill) in enumerate(blobs):
        if left > crop_width * MAX_TICK_LEFT:
            # Left to right, so nothing further along can be the tick either.
            break
        if height < floor:
            continue
        if not TICK_ASPECT[0] <= (right - left) / max(height, 1) <= TICK_ASPECT[1]:
            continue
        # A ring, not a blot and not a frame. Tile artwork is square often
        # enough to pass the test above; it is solid, a frame is hollow, and
        # the icon is neither.
        if not MIN_TICK_FILL <= fill <= MAX_TICK_FILL:
            continue
        return index
    return None


def shows_tick(prepared: np.ndarray) -> bool:
    """Whether a prepared badge crop actually contains the tick icon.

    The count is only ever drawn beside the tick, so the tick is the evidence
    that there is a count at all. Without this the reader answers a question it
    was never asked: given a crop of artwork and told to find text, Tesseract
    finds text, and the vertical lines in Warframe's tile frames are a very
    good "1". Three of eighteen counts on real captures were made up
    that way, one of them "111", and the numbers changed from run to run.

    Any tall-enough ink blob may be the tick, not only the largest: a smear of
    artwork bleeding into the crop can outweigh the icon without hiding it, and
    demanding the largest cost two real counts on the reference captures. What
    no stroke and no smear produces is a square one.
    """
    grey = (prepared if prepared.ndim == 2
            else cv2.cvtColor(prepared, cv2.COLOR_BGR2GRAY))
    return _tick(_blobs(grey), grey.shape[0], grey.shape[1]) is not None


#: Where the tile's own name sits, and what it is worth.
#:
#: The grid is fitted once over a whole recording, so a single tile's crop can
#: sit a few percent high or low - measured spread of the name's bottom edge
#: across 222 tiles from one folder: 0.805 to 0.996 of the crop height, a
#: drift of about a tenth of a tile. The badge is a tenth of a tile tall, so
#: that drift is the difference between reading the count and reading the
#: artwork above it.
#:
#: The name is a better ruler than the grid. Warframe bottom-aligns it inside
#: the tile - a name that wraps to two lines grows upwards - so its bottom
#: edge is at a fixed distance from the tile's bottom whatever the name says.
#: Measured against nine tiles with legible badges, anchored this way, the
#: badge's top edge sits at 0.803 tile-heights above the name's bottom with a
#: standard deviation of 0.0000. The grid gives the same number with a spread
#: of 0.05.
BADGE_ABOVE_NAME = (0.60, 0.86)   # bottom, top - in tile heights above the name

#: How wide the badge sits, as fractions of the tile, once the tile is aligned.
#: Measured extremes over the same tiles: 0.074 to 0.424. The margin either
#: side is deliberate - a region that clips a digit reads "12" as "1".
BADGE_ACROSS = (0.04, 0.47)


#: How far apart two printed lines of one name sit, as a fraction of the tile.
#: Measured on real captures: a wrapped name puts its second line about
#: 0.055 tile-heights below the first, while the gap up to the artwork is
#: several times that.
NAME_LINE_GAP = 0.075

#: The highest a printed name starts, as a fraction of the tile. Three lines is
#: the longest label the game produces ("Gauss Prime / Neuroptics / Blueprint")
#: and it still begins below this.
NAME_HIGHEST = 0.45


def _name_mask(tile_crop: np.ndarray) -> np.ndarray:
    """Where the printed name's ink is, whichever way round and however faint.

    Two things vary and both had been assumed. The polarity: a dark interface
    theme prints the name bright and blue, a light one prints it dark on a
    pale plate. And the contrast: a tile whose blueprint is already in the
    foundry is drawn dimmed, and its name is a light grey on a slightly
    lighter grey. A fixed threshold of 45 levels found nothing there, so the
    name could not be located at all - which is every four-word blueprint in
    real captures, and they are exactly the ones that were missing.

    Otsu picks the split from the plate itself, so faint is as findable as
    bold.
    """
    height = tile_crop.shape[0]
    blue, _green, red = cv2.split(tile_crop.astype(np.int16))
    luminance = cv2.cvtColor(tile_crop, cv2.COLOR_BGR2GRAY)
    plate = luminance[int(height * 0.6):]
    if plate.size and float(plate.mean()) > 140:
        level, _ = cv2.threshold(plate, 0, 255,
                                 cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # Otsu on a plate holding no text at all splits noise; a floor keeps
        # that from lighting up the whole crop.
        level = min(float(level), float(plate.mean()) - 6)
        return (luminance.astype(np.int16) < level).astype(np.uint8)
    return ((luminance.astype(np.int16) > 110)
            & ((blue - red) > 25)).astype(np.uint8)


def name_bottom(tile_crop: np.ndarray, expected: float | None = None) -> int | None:
    """The row where the tile's printed name ends, or None if none is legible.

    The name is bright and blue against artwork that is neither, which
    separates it from the picture above far more reliably than brightness
    alone: Prime artwork is gold and very bright.
    """
    height, width = tile_crop.shape[:2]
    if height < 20 or width < 20 or tile_crop.ndim != 3:
        return None
    mask = _name_mask(tile_crop)

    rows = mask.sum(axis=1)
    lit = np.nonzero(rows > width * 0.03)[0]
    if len(lit) == 0:
        return None
    # Two printed lines are one name. The gap between them is a few times the
    # gap inside a letter, and far smaller than the gap between a name and the
    # artwork above it, so merging across it groups a wrapped name into one
    # block whose bottom is the name's bottom - which is what the caller means.
    line_gap = max(4, int(height * NAME_LINE_GAP))
    groups: list[list[int]] = [[int(lit[0])]]
    for value in lit[1:]:
        if value - groups[-1][-1] <= line_gap:
            groups[-1].append(int(value))
        else:
            groups.append([int(value)])

    # Which run of lit rows is the name. Within a tile the answer is the
    # lowest: anything above it is artwork, or the row above's name bleeding
    # in. Given an expected position the answer is the nearest one instead,
    # because the caller is looking at a window deliberately taller than a
    # tile - and there the lowest run is as likely to be the *next* tile's
    # name. Taking it moved boxes onto the wrong tile and cost three stack
    # counts that had been right.
    if expected is None:
        return groups[-1][-1]
    return min((group[-1] for group in groups),
               key=lambda bottom: abs(bottom - expected))


def name_block(tile_crop: np.ndarray) -> tuple[int, int] | None:
    """(top, bottom) of the printed name, found rather than assumed.

    The band readers crop at fixed fractions of the tile - 0.66 of its height,
    0.55, 0.48 - which is a guess about how many lines the name takes. It holds
    for one line and mostly for two, and fails for the four-word names: the
    crop starts below the first line, so "Perigale Prime Blueprint" comes back
    as "ale Prime Blueprint" and a crop high enough to catch it catches the
    tile above's name as well.

    The block is already detectable - `name_bottom` finds it to anchor the
    badge - so the same run of lit rows can give the top edge too, and the crop
    can be the name rather than a guess about where the name is.
    """
    height, width = tile_crop.shape[:2]
    if height < 20 or width < 20 or tile_crop.ndim != 3:
        return None
    mask = _name_mask(tile_crop)
    # Only where a name can be. On a dimmed tile - a blueprint already in the
    # foundry - the artwork is darker than the plate too, so it satisfies the
    # mask and merges with the name into one block spanning most of the tile,
    # which is then rejected as "not a name". Even the longest label, three
    # printed lines, starts below this.
    mask[:int(height * NAME_HIGHEST), :] = 0

    rows = mask.sum(axis=1)
    lit = np.nonzero(rows > width * 0.03)[0]
    if len(lit) == 0:
        return None
    line_gap = max(4, int(height * NAME_LINE_GAP))
    groups: list[list[int]] = [[int(lit[0])]]
    for value in lit[1:]:
        if value - groups[-1][-1] <= line_gap:
            groups[-1].append(int(value))
        else:
            groups.append([int(value)])

    # The lowest block is the name: anything above it is artwork, or the row
    # above's name bleeding into a crop that is taller than a tile.
    block = groups[-1]
    top, bottom = block[0], block[-1]
    # A block that reaches most of the way up the tile is not a name, it is
    # artwork that happens to satisfy the mask.
    if bottom - top > height * 0.45 or bottom < height * 0.5:
        return None
    return top, bottom


#: The widest empty stretch, as a fraction of the tile's height, that still
#: counts as the gap between two words of one name. The gap between this
#: tile's name and its neighbour's is a plate margin on each side plus the gap
#: between the plates - 53 pixels at the narrowest on a 444-pixel tile, where
#: a word space is about 12.
NAME_WORD_GAP = 0.075


def name_columns(tile_crop: np.ndarray, block: tuple[int, int]) -> tuple[int, int] | None:
    """(left, right) of this tile's own name, so a neighbour's is left out.

    A tile is cut wider than the tile itself, because the badge and the
    picture want the margin, so the end of the name on the left and the start
    of the one on the right stand in the band: "Guandao Prime Blade" read as
    "Guandao Prime de Blade". That costs the reading its score against the
    right name, and a variant that lost the first letters has no such stray
    word, so it scores higher - 11 tiles of one Prime Parts recording went to
    the review as "does not clearly carry the word" for that reason.

    The name is the widest run of ink in the block's rows; runs closer than a
    word space belong together. Returns None when there is no run wide enough
    to be a name, which keeps the whole width.
    """
    height, width = tile_crop.shape[:2]
    rows = _name_mask(tile_crop)[block[0]:block[1] + 1]
    lit = np.nonzero(rows.sum(axis=0) > 0)[0]
    if len(lit) == 0:
        return None
    gap = max(4, int(height * NAME_WORD_GAP))
    runs = [[int(lit[0]), int(lit[0])]]
    for value in lit[1:]:
        if value - runs[-1][1] <= gap:
            runs[-1][1] = int(value)
        else:
            runs.append([int(value), int(value)])
    left, right = max(runs, key=lambda run: run[1] - run[0])
    if right - left < width * 0.15:
        return None
    # The same air the vertical crop leaves around a name.
    margin = int(height * 0.03)
    return max(0, left - margin), min(width, right + margin + 1)


def badge_box(tile_crop: np.ndarray) -> tuple[int, int, int, int] | None:
    """Where the stack badge sits in this tile, anchored on its name.

    Falls back to the tile's own proportions when no name is legible, which is
    what a tile clipped by the screen edge looks like.
    """
    height, width = tile_crop.shape[:2]
    anchor = name_bottom(tile_crop)
    if anchor is None:
        top = int(BADGE_REGION[1] * height)
        bottom = top + int(BADGE_REGION[3] * height)
    else:
        top = int(anchor - BADGE_ABOVE_NAME[1] * height)
        bottom = int(anchor - BADGE_ABOVE_NAME[0] * height)
    top = max(0, top)
    bottom = min(height, bottom)
    left = int(BADGE_ACROSS[0] * width)
    right = int(BADGE_ACROSS[1] * width)
    if bottom - top < 6 or right - left < 8:
        return None
    return left, top, right, bottom


#: How far a digit may sit from the one before it, in icon widths. The count is
#: set solid beside the tick; artwork that survives thresholding is not, and it
#: is the gap that tells them apart.
#:
#: Measured: across the 8 real two-digit counts in the badge fixtures the gap
#: between two figures is at most 0.36 icon widths. Artwork behind the badge
#: sat at 0.67 (Styanax) and 0.87 (Citrine Prime Blueprint, a blob of the
#: Warframe at the crop's right edge that passed every other test and turned a
#: stack of 2 into 24). 0.6 lies between the real maximum and the nearest
#: artwork, and changed no other fixture; the former 1.1 admitted both blobs.
DIGIT_GAP = 0.6

#: A digit's height against the tick's. The tick is drawn the same size as the
#: figures beside it, which makes it a ruler for them.
DIGIT_HEIGHT = (0.45, 1.25)

#: How far a figure's middle may sit from the tick's, in icon heights.
DIGIT_BASELINE = 0.25

#: How much a later figure may differ in height from the first one. The tick
#: is a ruler, but a loose one: it admits anything from 0.45 to 1.25 of its own
#: height, and tile artwork lands inside that range often enough. The figures
#: of a count are set in one size, so the first of them is the tighter ruler -
#: "a solid run of same-sized marks", which the comment above claimed and
#: nothing measured.
#:
#: Measured on Styanax Prime Blueprint at full resolution: the tick is 135 tall,
#: the "2" is 99, and two pieces of the Warframe's helmet behind it are 183 and
#: 144. Against the tick those are 1.36 and 1.07, so one passed; against the
#: figure they are 1.85 and 1.45. The badge said 2 and the scan recorded 21.
DIGIT_MATCH = 0.3

#: The narrowest a figure can be, as width over height. A stroke is not a
#: figure however tall it stands, and the height rules above cannot say so:
#: a stroke on the tick's own line at the tick's own height passes them all.
#:
#: Measured on every badge fixture at full resolution: the "1" is the
#: narrowest figure of the interface font at 0.34, every other digit lies
#: between 0.59 and 0.72. The stroke that reached this rule was the border
#: of the box the infinity badge is drawn in - 3 pixels wide by 195 tall,
#: 0.015 - and it did not merely misread: scaled to a figure's height its
#: width rounded to zero, `cv2.resize` refused the empty size, and the whole
#: scan failed with it.
DIGIT_ASPECT_MIN = 0.2


def badge_crop(tile_crop: np.ndarray) -> np.ndarray | None:
    """The badge area of a tile, thresholded and enlarged, or None.

    Returns None when the corner holds no badge, which is most tiles. Splitting
    this from the reading lets the crops be gathered first and read together:
    one tesseract process per tile in sequence is about a second each, and the
    pool does fourteen at a time.
    """
    if not has_badge(tile_crop):
        return None
    box = badge_box(tile_crop)
    if box is None:
        return None
    left, top, right, bottom = box
    crop = tile_crop[top:bottom, left:right]
    if crop.size == 0:
        return None
    prepared = _prepare(crop)
    if not shows_tick(prepared):
        return None
    return prepared


def parse_badge(text: str) -> int | None:
    """The count in a badge reading, or None when it holds no usable number."""
    match = _DIGITS.search(text or "")
    if not match:
        return None
    digits = match.group(0).replace(",", "").replace(".", "")
    if not digits.isdigit():
        return None
    value = int(digits)
    # A stack of one is not shown, and a five-digit count is a misread of the
    # artwork behind the badge.
    return value if 2 <= value <= 99999 else None


def read_badge(tile_crop: np.ndarray, engine) -> Quantity:
    """Read the stack count from a full tile image.

    Returns `count=None` when no badge is present — deliberately distinct from
    `count=1`. "No badge" means the item does not stack; "1" would claim a
    counted single, and the two lead to different conclusions about duplicates.
    """
    height, width = tile_crop.shape[:2]
    x = int(BADGE_REGION[0] * width)
    y = int(BADGE_REGION[1] * height)
    box_width = int(BADGE_REGION[2] * width)
    box_height = int(BADGE_REGION[3] * height)
    if box_width < 8 or box_height < 6:
        return Quantity(None, 0.0, "")

    crop = tile_crop[y:y + box_height, x:x + box_width]
    if crop.size == 0:
        return Quantity(None, 0.0, "")

    from wft.ocr import engine as ocr_mod

    prepared = _prepare(crop)
    if not shows_tick(prepared):
        return Quantity(None, 0.0, "")
    reading = engine.read(prepared, psm=ocr_mod.PSM_LINE)
    text = reading.text.strip()
    confidence = reading.confidence if reading.confidence is not None else 100.0

    match = _DIGITS.search(text)
    if not match or confidence < MIN_CONFIDENCE:
        return Quantity(None, confidence, text)

    digits = match.group(0).replace(",", "").replace(".", "")
    if not digits.isdigit():
        return Quantity(None, confidence, text)

    value = int(digits)
    # A stack of one is not shown, and a five-digit count is a misread of the
    # artwork behind the badge.
    if value < 2 or value > 99999:
        return Quantity(None, confidence, text)
    return Quantity(value, confidence, text)


def owned_from(tiles_with_quantity: list[Quantity]) -> int:
    """Total held, from the tiles showing the same item.

    Tiles times badge quantity. A tile with no badge counts as one, because the
    item does not stack — which is why `None` and `1` had to stay distinguishable
    all the way to here.
    """
    return sum(q.count if q.count is not None else 1 for q in tiles_with_quantity)

#: How tall a single figure is made before reading. Tesseract is documented as
#: wanting an x-height around 20-30 pixels and as treating anything under 8 as
#: noise; the badge on a 4K capture is about 20 and on the smaller copy about
#: 12, so both are brought to the same size rather than left to chance.
DIGIT_HEIGHT_PX = 28

#: White margin around each figure. Tesseract expects a page, not a glyph
#: pressed against the edge.
DIGIT_MARGIN_PX = 14


def digit_crops(prepared: np.ndarray) -> list[np.ndarray]:
    """One image per figure of the count, left to right, or [] if unreadable.

    Read one figure at a time rather than the count as a line. A stack count
    is one to three characters of a fixed interface font with no words around
    it, which is the case Tesseract is worst at: it is built for running text
    and leans on context a bare "12" does not have. Measured on the user's
    captures against counts read off the pictures by eye - as a line, 6 of 9
    correct; figure by figure, 9 of 9, none wrong.

    The tick icon is found and dropped here rather than cropped away, because
    cropping to the figures' x-range re-admits whatever overlaps it: the frame
    around Xaku Prime Chassis Blueprint's artwork reaches across the second
    figure's column, and a stack of 2 came back as 25. Segmenting once and
    handing over the marks themselves cannot do that.

    Only the tick leads a stack count. Some tiles carry a second icon, a small
    figure with a ring at its foot, sometimes with a number beside it: that
    number counts copies of the finished item already built (the tile's
    tooltip says "3 Crafted" on Lex Prime Blueprint, of which one is held). It
    is never read as a count; a tile without the tick holds one.
    """
    blobs = _blobs(prepared)
    icon = _tick(blobs, prepared.shape[0], prepared.shape[1])
    if icon is None:
        # `shows_tick` was satisfied and this is not, which cannot happen while
        # both ask `_tick`. Give up rather than read the crop whole: the icon
        # is a filled ring, and read as a character it comes back as a 9 -
        # turning a stack of 2 into 92 and one of 13 into 913 on the user's
        # own captures. A count nobody records costs one correction; a count
        # wrong by a factor of ten is not visible as an error at all.
        return []
    _left, icon_right, icon_height, icon_middle, _fill = blobs[icon]
    icon_width = icon_right - _left

    crops = []
    edge = icon_right
    #: The height of the first figure, once one is found. Every figure after it
    #: is measured against this rather than against the icon.
    figure_height = None
    for left, right, height, middle, _fill in blobs[icon + 1:]:
        # A count is a solid run of same-sized marks on one line beside the
        # tick. Each test threw out a real misreading: the gap catches artwork
        # further along the tile, the height catches the frame strokes, and
        # the line catches decoration directly above or below the number.
        if left - edge > icon_width * DIGIT_GAP:
            break
        if not (DIGIT_HEIGHT[0] <= height / max(icon_height, 1) <= DIGIT_HEIGHT[1]):
            continue
        if abs(middle - icon_middle) > icon_height * DIGIT_BASELINE:
            continue
        if (right - left) / max(height, 1) < DIGIT_ASPECT_MIN:
            # A stroke, not a figure. Skipped rather than ending the count:
            # the tile frame can run through the badge between two figures.
            continue
        if (figure_height is not None
                and abs(height - figure_height) > figure_height * DIGIT_MATCH):
            # Not the same size as the figure beside it, so not a figure.
            break
        glyph = prepared[max(0, middle - height // 2 - 2):middle + height // 2 + 2,
                         left:right]
        if glyph.size == 0:
            continue
        scale = DIGIT_HEIGHT_PX / max(glyph.shape[0], 1)
        glyph = cv2.resize(glyph, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_CUBIC)
        crops.append(cv2.copyMakeBorder(
            glyph, DIGIT_MARGIN_PX, DIGIT_MARGIN_PX,
            DIGIT_MARGIN_PX, DIGIT_MARGIN_PX,
            cv2.BORDER_CONSTANT, value=255))
        if figure_height is None:
            figure_height = height
        edge = right
    return crops


def assemble(readings: list[str]) -> int | None:
    """Turn per-figure readings into a count, or None if any figure failed.

    All or nothing on purpose. A missing figure does not make the count
    smaller, it makes it wrong - "12" with the 1 lost is 2, and 2 is a number
    the rest of the tool has no reason to doubt.
    """
    digits = ""
    for reading in readings:
        stripped = (reading or "").strip()
        if not stripped or not stripped[0].isdigit():
            return None
        digits += stripped[0]
    return parse_badge(digits)
