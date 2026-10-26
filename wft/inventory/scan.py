"""Reading a whole inventory recording into candidate holdings.

Ties together the pieces: sample frames by movement, collect OCR words onto one
virtual page, fit the grid once, drop screen-fixed UI text, then let each cell
propose its readings and pick the best against the item database.

Nothing here decides what the user owns. It produces candidates with evidence,
and the review step decides — because a wrong holding does not fail loudly, it
produces a confidently wrong recommendation.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from typing import NamedTuple

from rapidfuzz import fuzz, process

from wft.data import db as dbq
from wft.inventory import (calibrate as cal_mod, frames as frames_mod, ocrpool,
                           pagemodel as pm, quantity as quantity_mod)
from wft.ocr import engine as ocr_mod
from wft.relic import match as match_mod


def _marks(values) -> str:
    return ",".join("?" * len(values))


#: Below this the reading is not offered at all. Above CONFIDENT it is accepted
#: without review. Between the two it goes to the review queue with its crop.
MIN_SCORE = 82.0

#: Below this a reading is only accepted when it matches a name exactly. Set
#: where it is because three characters of OCR noise will find a plausible
#: partner among 3800 names, and a fuzzy match on that little text says nothing.
MIN_READING = 4
CONFIDENT_SCORE = 92.0

#: How far a winner must sit above the runner-up to count as settled. Two points
#: is small, and it only has to separate a decision from a coin toss: relic
#: names are built to differ by a single character, so "Lith 04 Relic" scored
#: 92.3 against Lith Z4, V4, N4, W4 and B4 simultaneously and the answer came
#: from the sort order. Measured on a full scan, three of 270 confident matches
#: had no lead at all, and two of those three were wrong.
MIN_LEAD = 2.0

#: A second route to confidence: a score short of CONFIDENT_SCORE, carried by a
#: margin wide enough that nothing else is close. Against 3800 tracked names a
#: twelve-point gap is not luck - "Chroma Pfime" reaches Chroma Prime at 91.7
#: with the next candidate at 72.8, and "Sun Moon" reaches Sun & Moon by 31.7.
#:
#: Both halves are needed. The margin alone lets "Specter A" through to Spectra
#: at 87.5 by 17.9, which is a gear item read badly rather than a weapon read
#: well; the floor at 88 is what excludes it. Measured over a full scan: 15 of
#: 62 review cases clear this bar and all 15 are correct.
CLEAR_WINNER_SCORE = 88.0
CLEAR_WINNER_LEAD = 12.0

#: How closely a reading must resemble one the user already dismissed. High,
#: because the cost of being wrong is silently dropping a real item: at 92 the
#: readings have to be near-identical, which is what repeated scans of the same
#: tile actually produce.
LEARNED_MATCH = 92.0


def _clears_confidence(score: float, lead: float) -> bool:
    """The confidence rule, without needing a Candidate to ask it."""
    return ((score >= CONFIDENT_SCORE and lead >= MIN_LEAD)
            or (score >= CLEAR_WINNER_SCORE and lead >= CLEAR_WINNER_LEAD))

#: How wide a kept tile picture is. The review shows it as a thumbnail in a
#: table and as a panel beside the options; neither needs the full 450px, and
#: at full size two hundred of them are fifty megabytes held for the duration.
EVIDENCE_WIDTH = 320

#: A cell needs this many sightings before it counts. One sighting can be a
#: transition frame; it cannot be checked against anything.
#:
#: Except in a still picture, where one sighting is all there will ever be. A
#: screenshot is one frame by definition, so this rule - written for video -
#: silently discarded every cell in one: a folder of screenshots produced an
#: empty review and no explanation, and a folder mixing both lost the
#: screenshot tabs while the recordings came through.
MIN_SIGHTINGS = 2

#: How much of the total job the sampling pass accounts for. Measured on the
#: user's 161-second 4K recording, timed per stage:
#:
#:     sampling              41s   24%
#:     reading               81s   47%
#:     re-reading names      27s   16%
#:     reading stack counts  23s   13%
#:
#: A constant rather than a measurement, because the number is only needed
#: while the second pass has not started and so cannot be measured. It is
#: wrong on any recording with a different ratio of movement to stillness, and
#: wrong by a little is what an estimate is for.
SAMPLING_SHARE = 0.24

#: What the passes after reading account for. Not the tenth they were first
#: assumed to be: they run over the review queue and every tile's badge, and
#: together they are 29% of the wall clock. That whole slice used to be
#: invisible, so the bar sat at 99% with "0s left" for the best part of a
#: minute - indistinguishable from a hang, which is the one thing the progress
#: display exists to rule out.
FINISHING_SHARE = 0.29

#: Stages that belong to that last slice.
FINISHING_STAGES = ("re-reading names", "checking repeats",
                    "reading stack counts")

#: Bumped whenever a change makes an old import's numbers wrong rather than just
#: less good. A recording is read once and its results live in the database
#: until someone imports again, so a correction to the reading does not reach
#: what was already written - and nothing said so.
#:
#: 2: badges are only read on tiles that can stack. Before this, a fixed piece
#:    of tile artwork was read as a count on Warframes, which do not stack:
#:    three different frames came back as exactly 4.
#: 3: tile crops are cut at the measured boundary rather than an offset
#:    calibrated on a compressed page, which is what the counts were read from.
#: Bumped whenever a change makes previously stored numbers wrong rather than
#: merely different. Revision 4 is the badge reader: counts written before it
#: were right about four times in fifteen and silently wrong once, so a
#: database still on 3 is holding numbers this reader would not produce.
#: 11: names are resolved against the characters OCR trades for one another
#:    even when the raw scores do not tie, which is what separates relics
#:    whose names differ by a roman numeral. On a relics capture
#:    a tile holding ten Requiem II was read as "Requiem Il Relic", resolved
#:    to Requiem I and never reached the database; it now arrives as itself.
#:    The same revision carries the vertical bar through `match.normalise`
#:    (see the comment on `_NOISE` there): a bar standing between words is
#:    what this font's capital I comes back as, and deleting it turned a tile
#:    holding nine Requiem I into "Requiem Relic" - the name of a resource in
#:    the decoy list - so the tile was dismissed as scenery sixteen times over.
#:    Third in the same revision: two tiles claiming one relic are now asked
#:    about instead of one being dropped. The drop cost two numbers at once -
#:    nine Requiem I vanished and twenty Requiem III were filed under Requiem
#:    I - and no number the reader can produce says which tile was misread,
#:    only the two crops do.
#:    All three land together and none has been written to a database yet, so
#:    they share a number rather than spending three on one release.
SCAN_REVISION = 11

#: How many frames to measure the tile grid on before falling back to sampling
#: it occasionally. Enough for a median to mean something on a short capture.
PITCH_VOTES = 7


@dataclass
class Candidate:
    """One inventory slot, as far as the recording shows it."""

    column: int
    row: int
    name: str
    score: float
    reading: str
    sightings: int
    part_unique: str | None = None
    item_unique: str | None = None
    #: The item this tile repeats, when another tile already matched it more
    #: convincingly. Carried explicitly rather than signalled by zeroing the
    #: lead: the lead is a measurement and destroying it to encode something
    #: else meant every later stage had to guess the case back out of the
    #: numbers, and got it wrong for repeats that scored under 100.
    repeat_of: str | None = None
    #: Carried from the decision: why this needs asking about.
    doubt: str | None = None
    #: The word of `name` the reading did not clearly carry, when that is the
    #: doubt. Data, not a sentence to be matched: the review puts the
    #: names that contain it after the names that do not. Always cleared with
    #: the doubt it belongs to.
    doubt_word: str | None = None
    #: Set when another tile claims this same relic and the pictures prove
    #: they are two tiles rather than one filed twice. Structural rather than
    #: readable, because the review has to treat these differently from every
    #: other question: it groups questions by their reading, and two contested
    #: tiles that happen to read alike would be answered once and have their
    #: counts added together - which is the loss this flag exists to prevent.
    contested: bool = False
    #: Which captures in the folder this tile was seen in, and whether any of
    #: them was a still. Two screenshots that overlap show the same tile twice,
    #: and without knowing which capture a tile came from there is no way to
    #: tell that from two copies of the item.
    sources: frozenset = frozenset()
    from_still: bool = False
    #: Rank read off the tile, when legible. Mastery is granted at 30 and kept
    #: for good, so this is the one thing in the picture that says an item has
    #: been finished with - including items later sold.
    rank: int | None = None
    #: Set when the tile is a relic. Relics are neither parts nor items - not a
    #: component of anything, and consumed rather than built - so they carry
    #: their own identifier and their own table.
    relic_unique: str | None = None
    refinement: str | None = None
    #: PNG of the tile as it appeared, for the review to show. Only kept for
    #: candidates that need one — a scan produces hundreds of cells and holding
    #: an image for every confident one would be megabytes for nothing.
    #:
    #: This is the thing that made the previous attempt's review usable: seeing
    #: the tile beside the guess is what lets someone decide in a second, where
    #: a bare string means squinting at a name and hoping.
    evidence: bytes | None = None
    #: Which sampled frame the crop came from, and where the tile sat in it.
    #: The crop alone answers "is this the right tile"; it cannot answer "why
    #: did it read that", because the answer is usually next door - a neighbour
    #: bleeding in, a dialog over the shelf, the tile half off screen. Seeing
    #: the whole frame with the tile ringed is what the previous tool did and
    #: what the user asked for by name.
    frame_index: int | None = None
    bbox: tuple[int, int, int, int] | None = None
    #: Stack count from the tile's badge. `None` means no badge was found,
    #: which is not the same as one: a badge is drawn only on stackable items,
    #: so its absence says "this does not stack" and the tile counts as one.
    quantity: int | None = None
    #: How far the winner scored above the next candidate. Relic names differ by
    #: one character by construction - 760 of the 772 have five or more
    #: neighbours within ten points - so a reading like "Lith 04 Relic" can hit
    #: 92 against five relics at once and pick whichever the sort put first.
    lead: float = 0.0
    #: A name the tile's own name band gave that disagrees with the reading.
    #:
    #: The two readings come from different evidence. The page pass assigns
    #: words to cells by where they landed after the scroll offset is worked
    #: out, so a name near a tile boundary in a badly registered frame can be
    #: filed under the tile below. The re-read crops this tile's box, snapped
    #: to its own name, and reads that. When they disagree the box is the
    #: better witness, but not by enough to overrule the other silently - so
    #: the disagreement becomes the question, with the box's answer offered.
    alternative: str | None = None
    #: (frame, centre x, top, bottom) of the box whose own name band was read
    #: as this tile's name.
    #:
    #: The strongest thing a scan can say about a tile: this rectangle, in this
    #: frame, spells this item. Everything else about the tile - the stack
    #: badge, the picture - is then taken from the same rectangle, so they
    #: cannot describe different tiles. They used to: a tile read as
    #: "Meso F5 Relic" was pictured as "Meso N17 Relic", the tile one row
    #: below, and recorded that neighbour's count of 20 instead of its own 9.
    verified_box: tuple[int, int, int, int] | None = None

    @property
    def display_name(self) -> str:
        """The name with the refinement, when it has one.

        Two refinements of a relic are two rows in every table this feeds, and
        without the suffix they read as the same row listed twice.
        """
        return dbq.relic_label(self.name, self.refinement)

    @property
    def confident(self) -> bool:
        """A high score with a margin, or a smaller score with a wide one.

        The score alone was the whole test, and it let through exactly the
        readings it should not: a tie at 92 is not evidence, it is a coin toss
        with four other faces. A tie belongs in the review, where the picture
        settles it in a second.

        The second route runs the other way. "Chroma Pfime" scores 91.7 - under
        the bar - but the next candidate is twenty points behind, and a gap that
        wide against 3800 names is an answer. Sending it to the review only
        makes someone confirm what the numbers already say.

        Neither route applies once something else has raised a doubt. A second
        tile matching an item already accounted for is a question whatever it
        scored, and so is a reading that lost the word naming the item — those
        are findings about *this* reading, not about how it compares to others,
        and no margin answers them.
        """
        if self.repeat_of or self.doubt:
            return False
        return _clears_confidence(self.score, self.lead)


@dataclass
class Progress:
    """Where a scan has got to, and what that implies about the rest.

    The estimate is deliberately based on measured throughput rather than a
    fixed rate: how long a frame takes depends on how much text is on it, and
    an inventory page is not uniformly full. A number that drifts is still far
    better than none — the question being answered is "is this working or has
    it hung", and for that even a rough figure settles it.
    """

    frames_done: int
    frames_total: int
    words: int
    elapsed: float
    cancelled: bool = False
    #: Which pass is running. They are separate phases, so one percentage across
    #: both would mislead — reading takes roughly twice as long as sampling.
    stage: str = "reading"
    #: Recent throughput in frames per second, smoothed. Set by the scanner.
    #: A whole-run average is misleading here: the first seconds of a recording
    #: are usually static, where only the cheap frame comparison runs and no OCR
    #: at all, so the average early on suggested 64 minutes for a job that took
    #: 18. What the user wants to know is when *this* will finish, which recent
    #: speed answers and lifetime speed does not.
    rate: float | None = None

    @property
    def fraction(self) -> float:
        """Progress within the current pass."""
        return min(1.0, self.frames_done / self.frames_total) if self.frames_total else 0.0

    @property
    def overall(self) -> float:
        """Progress through the whole job, both passes together.

        A bar that fills, resets and fills again reads as a restart, and the
        second fill is the one that takes the time. Measured on a 161-second
        recording: sampling runs about a minute, reading about ninety seconds,
        so the split is a third and two thirds rather than half each.
        """
        if self.stage == "sampling":
            return self.fraction * SAMPLING_SHARE
        if self.stage in FINISHING_STAGES:
            # Both finishing stages share the last slice, in the order they run.
            position = FINISHING_STAGES.index(self.stage)
            width = FINISHING_SHARE / len(FINISHING_STAGES)
            return (1.0 - FINISHING_SHARE) + width * (position + self.fraction)
        reading = 1.0 - SAMPLING_SHARE - FINISHING_SHARE
        return SAMPLING_SHARE + self.fraction * reading

    @property
    def overall_remaining(self) -> float | None:
        """Seconds left in the whole job, not just this pass.

        During sampling the reading pass has not started and its rate is
        unknown, so it is estimated from this one - reading is roughly twice as
        slow per frame, but runs over far fewer frames, and the two roughly
        cancel. A figure that is approximately right beats a figure that says
        one minute and is followed by another two.
        """
        pass_left = self.remaining_seconds
        if pass_left is None:
            return None
        if self.stage in FINISHING_STAGES:
            return pass_left
        if self.stage != "sampling":
            # Reading is followed by the finishing passes, which are roughly a
            # tenth of the job against reading's half.
            return pass_left * (1.0 + FINISHING_SHARE / (1.0 - SAMPLING_SHARE
                                                         - FINISHING_SHARE))
        sampled = self.elapsed + pass_left
        return pass_left + sampled * (1.0 - SAMPLING_SHARE) / SAMPLING_SHARE

    @property
    def remaining_seconds(self) -> float | None:
        """None until there is enough measured throughput to say anything.

        The finishing passes need proportionally more of a run before their
        rate means anything: their first tiles pay for loading frames that
        later tiles reuse, so an estimate from the first few said 28 minutes
        for work that took two. A tenth of the pass is enough to settle it.
        """
        if not self.frames_total:
            return None
        settled = (max(20, self.frames_total // 10)
                   if self.stage in FINISHING_STAGES else 20)
        if self.frames_done < settled:
            return None
        rate = self.rate if self.rate else (self.frames_done / self.elapsed
                                            if self.elapsed > 0 else 0)
        if rate <= 0:
            return None
        return max(0.0, (self.frames_total - self.frames_done) / rate)

    def describe(self) -> str:
        if self.cancelled:
            return "cancelled"
        remaining = self.remaining_seconds
        if remaining is None:
            return f"{self.stage} {self.frames_done}, estimating..."
        minutes, seconds = divmod(int(remaining), 60)
        left = f"{minutes}:{seconds:02d} left" if minutes else f"{seconds}s left"
        return (f"{self.stage} {self.fraction:.0%}  "
                f"({self.frames_done}/{self.frames_total}, {self.words} words, {left})")


@dataclass
class ScanResult:
    candidates: list[Candidate]
    frames_read: int
    words_total: int
    words_after_ui_filter: int
    cells_found: int
    grid: pm.Grid | None
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False
    #: The frames candidates point at, shrunk and JPEG-encoded, keyed by frame
    #: index. Held rather than left on disk: the sampled frames are a temporary
    #: artefact of one run and hundreds of 4K stills is not something to keep
    #: alive for a review. Only frames some candidate actually cites are kept,
    #: so the dictionary is tens of entries, not thousands.
    frames: dict = field(default_factory=dict)

    @property
    def confident(self) -> list[Candidate]:
        return [c for c in self.candidates if c.confident]

    @property
    def needs_review(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.confident]


#: Words that qualify a name without being one. A tile whose name wraps to two
#: lines can have its second line assigned as a cell of its own, and "Blueprint"
#: alone then scores 86 against Bo Blueprint - better than the full name
#: "Auroxium Alloy X 20 Blueprint" scores against anything, because the full
#: name belongs to a resource this tool does not track and so has no right
#: answer to find. Forty-nine such fragments reached one review queue, all of
#: them for tiles that should simply have been ignored.
_GENERIC_WORDS = {"blueprint", "prime", "component", "part", "set"}


def _is_fragment(text: str) -> bool:
    """True when a reading is only qualifiers and numbers, naming nothing.

    Numbers count as qualifiers here because a resource blueprint is written
    "Auroxium Alloy X 20 Blueprint", so the wrapped second line often arrives as
    "20 Blueprint" rather than "Blueprint" alone. Neither says what the item is.
    """
    words = text.lower().split()
    return bool(words) and all(
        word in _GENERIC_WORDS or word.strip("x").isdigit() or len(word) == 1
        for word in words)


def _ignored_names(connection: sqlite3.Connection) -> list[str]:
    """Names to recognise and dismiss — cosmetics, mostly.

    Needed because dismissing requires recognising. "Yareli Prime Helmet" is a
    helmet skin; matched against tracked names alone it lands on "Yareli Prime"
    with a high score and counts as a built Warframe. Comparing against the
    cosmetics as well lets the better match win, and the better match is the
    skin.
    """
    return [row["name"] for row in connection.execute("SELECT name FROM ignored_names")]


def _tracked_names(connection: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """Every name worth recognising, mapped to (part_unique, item_unique).

    The database is the filter. An inventory screen is mostly things this tool
    has no opinion about — ore, fish, gear, specters — and they simply fail to
    match. That replaces the previous attempt's 194-entry blocklist, which had
    to enumerate what to ignore and ended up discarding component names.
    """
    lookup: dict[str, tuple[str | None, str | None]] = {}
    # Both spellings, and the alias resolved to what it stands for. The
    # inventory shows "Gauss Prime Systems Blueprint" while the component is
    # stored as "Gauss Prime Systems"; leaving the alias out sent that reading
    # to "Gauss Prime Blueprint" — the Warframe blueprint rather than the part,
    # which is a different item that happens to share most of its name.
    # The excluded categories are excluded on both sides - items and their
    # parts. Only items were filtered before, which let 201 part rows through
    # for things the user does not track: "CHITEN MUTAGEN Blueprint" and its
    # kind are hybridisation resources, and they were being counted as
    # inventory because they happen to have a recipe.
    #
    # reward_other is the same case with a reason of its own. Forma, Kuva,
    # Ayatan sculptures and Riven slivers have to exist as relic rewards - Forma alone drops from 2180
    # relics and reading "not recognised" on the most common reward in the game
    # would be a failure. But they are consumables and currencies, not equipment:
    # they fluctuate constantly, Kuva is held in the thousands rather than ones,
    # and an inventory row claiming "Kuva 1/1" is noise in a list whose purpose
    # is showing which sets are incomplete.
    for row in connection.execute(
            "SELECT p.unique_name, p.name, COALESCE(p.alias_of, p.unique_name) AS target "
            "FROM parts p LEFT JOIN items i ON i.unique_name = p.item_unique "
            f"WHERE COALESCE(i.category, '') NOT IN ({_marks(dbq.SCAN_IGNORED_PART_CATEGORIES)})",
            dbq.SCAN_IGNORED_PART_CATEGORIES):
        lookup[row["name"]] = Ident(part=row["target"])
    for row in connection.execute(
            f"SELECT unique_name, name FROM items "
            f"WHERE category NOT IN ({_marks(dbq.SCAN_IGNORED_CATEGORIES)})",
            dbq.SCAN_IGNORED_CATEGORIES):
        lookup.setdefault(row["name"], Ident(item=row["unique_name"]))
    # Relics resolve to their own identifier. They used to map to (None, None),
    # which made them recognisable and unrecordable at the same time: the scan
    # found dozens per run and the import had nowhere to put them.
    for row in connection.execute(
            "SELECT name, MIN(unique_name) AS unique_name FROM relics "
            "GROUP BY name"):
        lookup.setdefault(row["name"], Ident(relic=row["unique_name"]))

    return lookup


#: Where a tile actually starts, relative to the grid's row position, as a
#: fraction of the row pitch. The grid is fitted to the OCR words - the item
#: names - and a name sits at the *bottom* of its tile, so the row position
#: lands near the end of a tile rather than its start. Cutting at the row
#: position gave a picture with one tile's name at the top and the next tile's
#: badge below it, which made the badge reader attribute counts to the wrong
#: item entirely.
TILE_TOP_OFFSET = -0.58


class _FrameCache:
    """Frames decoded once, for the passes that share them.

    Everything after the OCR works frame by frame: the sweep, the second look,
    the evidence pass, the stack badges and the context pictures each walk the
    same few hundred JPEGs, and each used to decode its own copy — six to
    eight decodes per frame at sixteen milliseconds each, most of a minute of
    a scan spent re-reading files that had just been read.

    Bounded, because the frames are big: a 4K frame decodes to ~17 MB, so
    sixteen of them are a quarter of a gigabyte and a whole recording would be
    ten times that. The passes group their work by frame index, so a handful
    of entries catches nearly every repeat.

    The arrays handed out are shared: callers crop and read, never draw. Every
    consumer today allocates its own output — cvtColor, resize, threshold and
    friends all copy — which is what makes the sharing safe; a pass that needs
    to write on a frame must take a `.copy()` first.

    The OCR pool forks while a cache may be holding frames. Fine by
    construction: the workers never touch these objects, so copy-on-write
    keeps the buffers shared rather than duplicated.
    """

    def __init__(self, limit: int = 16):
        self._frames: dict = {}   # path -> ndarray, kept in recency order
        self._phases: dict = {}   # (path, pitch) -> float | None
        self._limit = limit
        self.hits = 0
        self.misses = 0

    def frame(self, path):
        """The decoded frame, or None where the file cannot be read."""
        import cv2

        image = self._frames.pop(path, None)
        if image is not None:
            self.hits += 1
            self._frames[path] = image  # back to most recently used
            return image
        self.misses += 1
        image = cv2.imread(path)
        if image is not None:
            self._frames[path] = image
            while len(self._frames) > self._limit:
                del self._frames[next(iter(self._frames))]
        return image

    def gap_phase(self, path, image, pitch: float) -> float | None:
        """`pagemodel.gap_phase`, remembered per frame.

        Pure in (frame, pitch), and asked about the same frame by three
        different passes. The answers are single floats, so they outlive the
        frames themselves.
        """
        key = (path, pitch)
        if key not in self._phases:
            self._phases[key] = pm.gap_phase(image, pitch)
        return self._phases[key]


#: Where the printed name's bottom edge sits inside a tile, as a fraction of
#: the tile's height. Derived rather than guessed: the badge's top edge is
#: 0.803 tile-heights above the name (measured, standard deviation zero) and
#: sits about 0.03 below the tile's top, which puts the name's bottom at 0.83.
NAME_BOTTOM_IN_TILE = 0.83

#: How far the snap is allowed to move a tile. Beyond a third of a tile the
#: thing found is more likely the neighbour's name than this one's, and a wrong
#: snap is worse than no snap: it would take a box that overlaps the right tile
#: and put it squarely on the wrong one.
MAX_NAME_SHIFT = 0.34


def _name_shift(image, centre_x: float, top: float, pitch: float,
                half: float) -> int:
    """How far to move a tile box so its name lands where a name belongs.

    Returns 0 when no name is legible, which is what a tile clipped by the
    screen edge or hidden behind a dialog looks like - and in that case the
    grid's own answer is the best one available.
    """
    from wft.inventory import quantity as quantity_mod

    height, width = image.shape[:2]
    y0 = int(max(0, top - pitch * MAX_NAME_SHIFT))
    y1 = int(min(height, top + pitch * (1.0 + MAX_NAME_SHIFT)))
    x0 = int(max(0, centre_x - half))
    x1 = int(min(width, centre_x + half))
    if y1 - y0 < 20 or x1 - x0 < 20:
        return 0
    window = image[y0:y1, x0:x1]
    # Where the name would be if the grid were exactly right, in window
    # coordinates. Without it the search takes the lowest text in the window,
    # which past the tile's bottom edge is the next tile's name.
    wanted_in_window = (top + pitch * NAME_BOTTOM_IN_TILE) - y0
    found = quantity_mod.name_bottom(window, expected=wanted_in_window)
    if found is None:
        return 0
    # Where the name sits now, in the frame; where it should sit, given the box
    # the grid proposed.
    actual = y0 + found
    wanted = top + pitch * NAME_BOTTOM_IN_TILE
    shift = int(round(actual - wanted))
    if abs(shift) > pitch * MAX_NAME_SHIFT:
        return 0
    return shift


def _visit_tiles(wanted, cells, grid, frame_files, scrolls, handler,
                 pad: float = 0.12, require_full: bool = False,
                 notes: list | None = None, sightings: int = 1,
                 drift: list | None = None, cache=None) -> None:
    """Call `handler(candidate, tile_image)` for each wanted cell.

    The frame chosen for a tile is the one where it sat furthest from the screen
    edge. A tile half out of view reads badly and looks worse.

    `sightings` asks for more than one frame per tile, best first. One frame is
    right for a picture and wrong for a number: the most central sighting can
    still be the one with a dialog over it, and a badge read through the search
    bar comes back as something plausible rather than as nothing. On the user's
    own captures that turned a stack of 25 into 1.

    `require_full` skips a tile that does not fit on screen instead of clipping
    it. The badge sits at the very top, and above the topmost row of tiles is
    the HUD - so a clipped tile does not yield "no badge", it yields the credit
    counter read as a stack count. A picture, by contrast, is still worth
    showing when it is cut off, which is why this is a choice and not a rule.
    """
    if not wanted or grid is None:
        return
    if cache is None:
        cache = _FrameCache()

    # The frame height, read from a frame rather than assumed. It was hardcoded
    # to 2160, which is right for this user's 4K recordings and wrong for every
    # other resolution - and wrong quietly: tiles near the bottom edge would
    # score as the most central sighting, so the crop chosen for a badge or a
    # picture would systematically be the worst one available.
    frame_height = 0
    for path in frame_files.values():
        probe = cache.frame(path)
        if probe is not None:
            frame_height = probe.shape[0]
            break
    if not frame_height:
        # No frame could be read back. Unlikely, and silent if unreported: the
        # review would open with no pictures and every badge missing, with
        # nothing to say why.
        if notes is not None:
            notes.append("no frames could be re-read — review pictures and "
                         "stack counts are missing for this scan")
        return

    # Two pitches, and they are not the same quantity. The fitted one lives on
    # the virtual page and decides which page row a tile belongs to; the
    # measured one is what a tile is on screen and decides how tall a crop is.
    # Using the fitted pitch as a crop height is what lost every stack count in
    # a folder import: the badge window is a fraction of the tile's height, so
    # a pitch ten percent out moves it by most of a badge.
    screen_pitch = grid.screen_row_pitch or grid.row_pitch
    TILE_TOP_FROM_ROW = TILE_TOP_OFFSET * grid.row_pitch
    by_frame: dict[int, list] = {}
    best_frames: dict[tuple, int | None] = {}
    for key, words in cells.items():
        candidate = wanted.get(key)
        if candidate is None:
            continue
        # Rank the sightings by how centrally the tile sits in each frame.
        ranked: list[tuple[float, int]] = []
        for word in words:
            scroll = scrolls.get(word.frame_index)
            if scroll is None:
                continue
            # The tile's own top edge, not the name's - the name sits near the
            # bottom of its tile, so measuring the name overstates how much of
            # the tile is on screen by more than half a row.
            screen_y = word.virtual_y - scroll + TILE_TOP_OFFSET * grid.row_pitch
            margin = min(screen_y, frame_height - screen_y)
            ranked.append((margin, word.frame_index))
        ranked.sort(reverse=True)
        chosen: list[int] = []
        for _margin, index in ranked:
            if index not in chosen:
                chosen.append(index)
            if len(chosen) >= max(1, sightings):
                break
        # The first is the best, and it is the one the picture comes from.
        # Without this the box drawn on the review would point into whichever
        # frame happened to be visited last, which is the worst of the set.
        best_frames[key] = chosen[0] if chosen else None
        for index in chosen:
            by_frame.setdefault(index, []).append((key, candidate))

    for frame_index, entries in by_frame.items():
        path = frame_files.get(frame_index)
        if not path:
            continue
        image = cache.frame(path)
        if image is None:
            continue
        height, width = image.shape[:2]
        scroll = scrolls.get(frame_index, 0.0)

        # Prefer the boundary this frame actually shows. Falling back to the
        # offset keeps a frame with a dialog over it usable.
        gap = cache.gap_phase(path, image, screen_pitch)

        for (column, row), candidate in entries:
            centre_x = grid.column_phase + column * grid.column_pitch
            top = grid.row_phase + row * grid.row_pitch - scroll + TILE_TOP_FROM_ROW
            half = grid.column_pitch * (0.5 + pad)
            if gap is not None:
                # Snap to the *nearest* measured boundary, not the one above.
                # The inferred top sits close to a real boundary by
                # construction, so rounding one way turns a few pixels of fit
                # error into a whole tile of displacement - the crop would show
                # the tile above, and its badge with it.
                remainder = (top - gap) % screen_pitch
                if remainder > screen_pitch / 2:
                    remainder -= screen_pitch
                raw_y0 = int(top - remainder)
            else:
                raw_y0 = int(top)
            raw_y1 = int(raw_y0 + screen_pitch)
            # Snap to the tile's own name. The grid is fitted once over a
            # whole recording, so an individual tile's box can sit a third of a
            # tile out - measured on real captures, the box for Meso G8
            # caught the row below's stack badge and missed its own. The name
            # is bottom-aligned inside the tile whatever it says, which makes
            # it a per-tile ruler where the grid is only an average one.
            shift = _name_shift(image, centre_x, raw_y0, screen_pitch, half)
            # How far the grid was out for this tile. Collected because it is
            # the measurement nothing had: the scroll compression that made the
            # virtual page 0.9% short went unnoticed for the project's whole
            # life, and this is exactly the signal that would have shown it.
            if drift is not None and shift is not None:
                drift.append(shift / screen_pitch)
            if shift:
                raw_y0 += shift
                raw_y1 += shift

            if require_full and (raw_y0 < 0 or raw_y1 > height):
                continue
            x0 = max(0, int(centre_x - half))
            x1 = min(width, int(centre_x + half))
            y0 = max(0, raw_y0)
            y1 = min(height, raw_y1)
            if x1 - x0 < 20 or y1 - y0 < 20:
                continue
            # Where the tile sat, so a picture can be pointed at later. Kept
            # on the candidate rather than passed to the handler: two of the
            # three handlers do not want it, and the one that does wants it
            # for tiles the others visited.
            if best_frames.get((column, row)) == frame_index:
                candidate.frame_index = frame_index
                candidate.bbox = (x0, y0, x1, y1)
            handler(candidate, image[y0:y1, x0:x1])


#: How well a position must read in its runner-up frame before that answer is
#: taken. The best frame said nothing, so this is the second opinion on a tile
#: that may not be there at all.
SECOND_LOOK_FLOOR = 97.0

#: How well a longer name must read before it is preferred to a shorter one it
#: contains. Below this the longer reading is more likely to be two tiles run
#: together than one tile read fully.
NAME_FLOOR = 85.0


def _contains_words(longer: str, shorter: str) -> bool:
    """True when every word of `shorter` appears in `longer`, in order.

    The test for a truncated reading. Prime part names lose the middle word
    far more often than the last one, so a prefix test would miss the case
    this exists for.
    """
    if len(longer) <= len(shorter):
        return False
    wanted = shorter.casefold().split()
    have = longer.casefold().split()
    position = 0
    for word in wanted:
        while position < len(have) and have[position] != word:
            position += 1
        if position >= len(have):
            return False
        position += 1
    return True


def _reading_fits_name(name: str, reading: str) -> bool:
    """Is this reading what is left of `name` after OCR dropped the front?

    The page pass reads a tile name as part of a whole 4K page, and over bright
    artwork it loses the first word outright: the tile that is
    "Xaku Prime Neuroptics Blueprint" comes back as "Neuroptics Blueprint" and
    the one that is "Perigale Prime Blueprint" as "ale Prime Blueprint". The
    matcher then does what it is asked to and finds a real item that fits what
    is left - "Ash Neuroptics Blueprint", "Aklex Prime Blueprint" - both of
    which score in the nineties, so nothing objects. Four tiles were recorded
    as items the user does not own this way.

    `_contains_words` cannot catch it, because it compares the two *names* and
    "Ash" is not a word of "Xaku Prime Neuroptics Blueprint". The comparison
    that works is against the *reading*: every word of it must be a word of
    the longer name, allowing for OCR damage ("Neuroptlcs") and for the cut
    landing mid-word ("ale" of "Perigale").
    """
    words = name.casefold().split()
    for part in reading.casefold().split():
        if len(part) < 3:
            # Too short to be evidence of anything - "e" and "1" sit inside
            # every name there is.
            return False
        if not any(fuzz.ratio(part, word) >= 80 or (len(part) >= 3 and part in word)
                   for word in words):
            return False
    return True


def _dump_state(stage, **values) -> None:
    """Append a snapshot of the scan's state, when asked.

    Set WFT_DEBUG_STATE to a file path and run the same scan twice: any line
    that differs between the two files is a place where the scan's answer
    depends on something other than its input. Two runs over the same folder
    disagreed about four relics, and guessing which stage lost them was how
    the previous evening went.
    """
    path = os.environ.get("WFT_DEBUG_STATE")
    if not path:
        return
    with open(path, "a") as handle:
        for key in sorted(values):
            handle.write(f"{stage} {key} {values[key]!r}\n")


def _fingerprint(parts) -> str:
    """A short digest of anything that should not change between two runs."""
    import hashlib

    return hashlib.md5("|".join(parts).encode()).hexdigest()[:8]


def _dump_crops(stage, jobs, owners, texts, position) -> None:
    """Fingerprint every crop and what it read, when asked.

    Paired with `_dump_state`: comparing two runs' crop hashes separates "the
    reader was handed different pixels" from "the reader answered differently",
    which are different bugs and were confused for each other twice.
    """
    if not os.environ.get("WFT_DEBUG_STATE"):
        return
    seen: dict = {}
    for index, owner in enumerate(owners):
        seen.setdefault(position(owner), []).append(
            (_fingerprint([jobs[index].hex()]), texts.get(index, "")))
    _dump_state(stage, read=sorted(seen.items()))


def _report_sweep(owners, texts, boxes=None) -> None:
    """Print what the band reader saw at every position, when asked.

    Set WFT_DEBUG_SWEEP to a comma-separated list of "column:row" to see only
    those, or to "all". This exists because the question "was that tile read
    at all, and as what" came up four times in one evening and each time it
    was answered by hand-patching this function.
    """
    wanted = os.environ.get("WFT_DEBUG_SWEEP")
    if not wanted:
        return
    keys = None
    if wanted != "all":
        keys = {tuple(int(part) for part in one.split(":"))
                for one in wanted.split(",") if ":" in one}
    best: dict[tuple[int, int], str] = {}
    for index, (column, row, _frame) in enumerate(owners):
        if keys is not None and (column, row) not in keys:
            continue
        text = texts.get(index) or ""
        if len(text) > len(best.get((column, row), "")):
            best[(column, row)] = text
    for key in sorted(keys or best, key=lambda k: (k[1], k[0])):
        box = (boxes or {}).get(key)
        where = (f" frame {box[0]} x{box[1]} y{box[2]}..{box[3]}" if box else "")
        print(f"SWEEP c{key[0]} r{key[1]}:{where} "
              f"{best.get(key, '(position never read)')[:46]!r}", flush=True)


def _choose_offer(options: list) -> tuple:
    """The decision to start from among those one tile's variants produced.

    The top score decides who may win, the number of variants that reached
    the same name decides which of them does, and only a tie there falls back
    to the order the variants came in. A tile is read in 36 variants, and on
    "Meso V15 Relic [Radiant]" two of them read "V1%", which scored 100 for
    "Meso V1 Relic", while five "levelled" ones scored 100 for "Meso V15
    Relic"; `max` kept the first, so a relic that is not held was recorded and
    the one that is was offered as gone. Counting
    only the top-scoring decisions matters: counting all of them would still
    have picked V1 (8 against 5), because the lower-scoring variants are
    mostly the same truncated misreading.

    Not "the longer name wins": that rule was measured to pull 170 name groups
    the wrong way (see `_one_tile_per_relic`).
    """
    top = max(o[0].score for o in options)
    leaders = [o for o in options if o[0].score == top]
    votes: dict = {}
    for decision, _frame in leaders:
        votes[decision.name] = votes.get(decision.name, 0) + 1
    most = max(votes.values())
    return next(o for o in leaders if votes[o[0].name] == most)


def _second_look(offers, boxes, spare_frame, owners, grid, screen_pitch, half,
                 frame_files, scrolls, names, ignored, decoy_words, learned,
                 notes, cache=None) -> int:
    """Read the positions that came back with nothing in a second frame.

    The last rows of a capture are the ones this is for. There is one frame
    showing them - the one the recording stopped on - and if it is smeared or
    has the cursor across it, every column of those rows reads as noise at
    once. Measured with the sweep's own debug hook, that is exactly what
    happens to the relics capture's last two rows, and it is why Requiem III
    and Wyrm produce no reading at all rather than a wrong one.
    """
    silent = sorted({(column, row) for column, row, _f in owners} - set(offers))
    if not silent:
        return 0
    if cache is None:
        cache = _FrameCache()
    grouped: dict[int, list] = {}
    for key in silent:
        frame_index = spare_frame.get(key)
        if frame_index is not None:
            grouped.setdefault(frame_index, []).append(key)
    tile_jobs: list = []
    tile_meta: list = []
    for frame_index, keys in grouped.items():
        path = frame_files.get(frame_index)
        image = cache.frame(path) if path else None
        if image is None:
            continue
        height, width = image.shape[:2]
        scroll = scrolls.get(frame_index, 0.0)
        gap = cache.gap_phase(path, image, screen_pitch)
        for column, row in keys:
            centre_x = grid.column_phase + column * grid.column_pitch
            top = (grid.row_phase + row * grid.row_pitch - scroll
                   + TILE_TOP_OFFSET * grid.row_pitch)
            if gap is not None:
                remainder = (top - gap) % screen_pitch
                if remainder > screen_pitch / 2:
                    remainder -= screen_pitch
                top -= remainder
            shift = _name_shift(image, centre_x, int(top), screen_pitch,
                                half) or 0
            y0, y1 = int(top) + shift, int(top + screen_pitch) + shift
            x0 = max(0, int(centre_x - half))
            x1 = min(width, int(centre_x + half))
            if y0 < 0 or y1 > height or x1 - x0 < grid.column_pitch * 0.6:
                continue
            tile_jobs.append(image[y0:y1, x0:x1].copy())
            tile_meta.append((column, row, frame_index, int(centre_x), y0, y1))
    if not tile_jobs:
        return 0
    found = 0
    # Collect first, then decide in a fixed order.
    #
    # `read_tiles` yields as workers finish, and this loop used to act on each
    # result as it arrived: it takes the first answer that clears the floor and
    # only replaces it on a strictly better score, so two readings of one
    # position that score the same were decided by which process finished
    # first. Measured over six runs of the same folder, that flipped
    # "Lith O3 Relic" against "Lith Q3 Relic" - both scoring exactly 100 - and
    # with it whether a tile at (2, 25) existed at all, because `decide_cell`
    # carries `learned` forward and a different order teaches it different
    # things. The sweep above already collects before it decides; this is the
    # one place that did not.
    produced: dict[int, list[str]] = {}
    for sequence, reads in ocrpool.read_tiles(tile_jobs, _sweep_bands, psm=6):
        produced[sequence] = reads
    owns: list = []
    texts: dict[int, str] = {}
    for sequence, meta in enumerate(tile_meta):
        for text in produced.get(sequence, []):
            if text:
                texts[len(owns)] = text
            owns.append(meta)
    if not owns:
        return 0
    _dump_crops("second-look",
                _band_jobs_for_dump(tile_jobs)
                if os.environ.get("WFT_DEBUG_STATE") else [],
                owns, texts, lambda o: o[:2])
    for index in range(len(owns)):
        text = texts.get(index)
        if not text:
            continue
        column, row, frame_index, centre_x, y0, y1 = owns[index]
        decision = decide_cell([" ".join(text.split())], names, ignored,
                               decoy_words, learned)
        if decision.name is None:
            continue
        # A high bar, because this is the frame that was passed over. A
        # position reads as nothing for two reasons - the frame is bad, which
        # this fixes, or there is no tile there at all, which it must not
        # invent. Measured at 90: relics read alone went from 51 of 55 to 54,
        # and in a folder from 54 to 52, because a doubtful reading here
        # becomes a second tile of a relic that already exists and the
        # dedup then keeps the wrong one.
        if decision.score < SECOND_LOOK_FLOOR:
            continue
        current = offers.get((column, row))
        if current and current[0][0].score >= decision.score:
            continue
        offers[(column, row)] = [(decision, frame_index)]
        boxes[(column, row)] = (frame_index, centre_x, y0, y1)
        found += 1
    if found:
        notes.append(f"{found} tile(s) read in a second frame after their "
                     f"first showed nothing")
    return found


def _sweep_frames(candidates, cells, grid, frame_files, scrolls, still_frames,
                  frame_source, lookup, ignored, decoy_words, learned, notes,
                  sharpness=None, progress=None, cache=None) -> int:
    """Read the name band at every tile position, not only where words landed.

    This is the fix for the order the pipeline had it in. The page pass reads a
    whole 4K frame at once, which over tile artwork produces things like
    ``'& \\ Calib Prime [30]'`` and ``'\\ ) ) W 9 D [ gth'``; whatever fails to
    resolve is dropped, and the tile stops existing. The band reader - the same
    crop, thresholded, read alone at PSM 6 - gets "Caliban Prime [30]" and
    "Dagath [30]" off those very tiles. It was only ever offered the tiles the
    page pass had already recognised, so the strongest reader in the system was
    allowed to work on the cases that were already solved.

    Measured on three screenshots of a Warframes tab: the whole
    pipeline found 16 tiles of 43; one screenshot swept this way gave 12 clean
    names in four rows, "Yareli Prime [30]" among them - a tile the scan had
    recorded as "Yareli" and then queried as a duplicate.

    Positions come from the grid and from the boundary each frame actually
    shows, so a tile needs no legible word anywhere to be found. A position the
    page pass already claimed is left alone: this adds tiles, it does not
    re-decide them.

    Found tiles are registered in `cells` with a synthetic word, which is what
    lets everything downstream - pictures, stack badges, the re-read - treat
    them as tiles the page pass had seen all along.
    """
    import cv2

    if grid is None or not frame_files:
        return 0

    names = list(lookup)
    held = {(c.column, c.row): c for c in candidates}
    # One entry per position: the tile crop itself, and who it belongs to.
    # The 36-variant fan-out happens in the pool (see _sweep_bands); holding
    # ~420 tile copies is ~250MB, a quarter of what the encoded variants
    # used to weigh, and each is a copy so it does not pin its whole frame
    # in the cache.
    tile_jobs: list = []
    tile_meta: list[tuple[int, int, int]] = []
    boxes: dict[tuple[int, int], tuple[int, int, int, int]] = {}

    screen_pitch = grid.screen_row_pitch or grid.row_pitch
    half = grid.column_pitch * (0.5 + 0.12)

    # Which frame shows each position best, worked out before any pixel is
    # read. A tile in a recording appears in a dozen frames; reading its band
    # in every one of them would be a dozen times the work for one answer, and
    # the answer would come from whichever frame happened to be last. So the
    # positions are enumerated from the grid and the scroll alone - no image
    # needed - and each is read once, in the frame where it sits furthest from
    # a screen edge.
    if cache is None:
        cache = _FrameCache()
    probe = cache.frame(next(iter(sorted(frame_files.values()))))
    if probe is None:
        return 0
    frame_height, frame_width = probe.shape[:2]

    # What counts as sharp, per capture. Relative because the absolute number
    # depends on what is on screen - a page of relics is all one shape, a page
    # of Warframes is not - and per capture because a screenshot is perfectly
    # still and a recording never is. One threshold across a folder is set by
    # whichever capture is sharpest and then rejects the others wholesale, so
    # the frame chosen for a tile is no longer the best that capture had.
    #
    # Measured: in a folder the relics capture lost six tiles that it read on
    # its own, in scattered cells with their neighbours intact - which is a
    # frame-choice failure, not a geometry one.
    per_source: dict[int, list[float]] = {}
    for index, clarity in (sharpness or {}).items():
        per_source.setdefault(frame_source.get(index, 0), []).append(clarity)
    sharp_enough: dict[int, float] = {}
    for source, values in per_source.items():
        values.sort()
        sharp_enough[source] = values[int(len(values) * 0.6)] * 0.75

    best_frame: dict[tuple[int, int], tuple[tuple, int]] = {}
    #: The runner-up frame per position, for one that reads as nothing.
    spare_frame: dict[tuple[int, int], int] = {}
    for frame_index in frame_files:
        scroll = scrolls.get(frame_index)
        if scroll is None:
            continue
        row = int(round((scroll - TILE_TOP_OFFSET * grid.row_pitch
                         - grid.row_phase) / grid.row_pitch)) - 1
        while True:
            top = (grid.row_phase + row * grid.row_pitch - scroll
                   + TILE_TOP_OFFSET * grid.row_pitch)
            if top > frame_height:
                break
            # The name sits at the bottom of a tile, so a tile whose top is
            # off screen still shows what it is. Requiring the whole tile meant
            # the last row of a capture was never enumerated: the companions
            # recording ends with Sweeper, Taxon, Verglas Prime and Wyrm, and
            # the sweep never looked at them.
            #
            # The badge is another matter - it sits at the very top - so a
            # clipped box may not answer for one. `whole` carries that, and it
            # outranks everything else in the frame choice.
            whole = top >= 0 and top + screen_pitch <= frame_height
            band_shown = (top > -screen_pitch * 0.5
                          and top + screen_pitch * NAME_BOTTOM_IN_TILE
                          <= frame_height)
            if whole or band_shown:
                # Sharp enough, then most central. Ranking on sharpness alone
                # was measured and was worse: it reads names no better and
                # costs stack counts, because the sharpest frame in a
                # recording is often one where the tile sits against an edge.
                # A threshold keeps the smeared frames out without letting
                # clarity decide between two frames that are both fine.
                clarity = (sharpness or {}).get(frame_index, 0.0)
                floor = sharp_enough.get(frame_source.get(frame_index, 0), 0.0)
                rank = (clarity >= floor,
                        min(top, frame_height - (top + screen_pitch)))
                for column in range(grid.columns):
                    key = (column, row)
                    rank = (whole,) + rank
                    if key not in best_frame or rank > best_frame[key][0]:
                        # Keep the one it displaces. At the end of a capture
                        # there is one frame to choose from and it is the one
                        # the scroll stopped on; when that frame reads as
                        # nothing, the runner-up is all there is.
                        if key in best_frame:
                            spare_frame[key] = best_frame[key][1]
                        best_frame[key] = (rank, frame_index)
                    elif key not in spare_frame:
                        spare_frame[key] = frame_index
            row += 1

    wanted_by_frame: dict[int, list[tuple[int, int]]] = {}
    for (column, row), (_rank, frame_index) in best_frame.items():
        wanted_by_frame.setdefault(frame_index, []).append((column, row))

    for done, frame_index in enumerate(sorted(wanted_by_frame)):
        if progress:
            progress(done)
        path = frame_files.get(frame_index)
        if not path:
            continue
        image = cache.frame(path)
        if image is None:
            continue
        height, width = image.shape[:2]
        scroll = scrolls.get(frame_index, 0.0)
        # The boundary this frame actually shows. Without it the enumeration
        # would inherit exactly the drift that put a crop one row off.
        gap = cache.gap_phase(path, image, screen_pitch)
        for column, row in wanted_by_frame[frame_index]:
            centre_x = grid.column_phase + column * grid.column_pitch
            top = (grid.row_phase + row * grid.row_pitch - scroll
                   + TILE_TOP_OFFSET * grid.row_pitch)
            if gap is not None:
                remainder = (top - gap) % screen_pitch
                if remainder > screen_pitch / 2:
                    remainder -= screen_pitch
                top -= remainder
            shift = _name_shift(image, centre_x, int(top), screen_pitch,
                                half) or 0
            y0, y1 = int(top) + shift, int(top + screen_pitch) + shift
            # Clamped sideways, not skipped. Frames are written cropped to the
            # tile area - 2611 pixels wide on real captures, not 3840 -
            # and the padded box for the rightmost column runs 13 pixels past
            # that edge. Requiring the whole box to fit threw away the entire
            # last column: Atlas, Excalibur, Loki, Nova, Saryn, Vauban and
            # Zephyr, every one of them plainly legible on screen.
            #
            # The row still has to fit whole. That is a different question -
            # the badge sits at the very top of a tile, and above the topmost
            # row is the HUD.
            x0, x1 = max(0, int(centre_x - half)), min(width, int(centre_x + half))
            clipped = y0 < 0 or y1 > height
            if clipped:
                # Read the name off what is on screen. The badge is another
                # matter and a clipped box may not answer for one, so it does
                # not become this tile's box.
                y0, y1 = max(0, y0), min(height, y1)
            if y1 - y0 < screen_pitch * 0.5 or x1 - x0 < grid.column_pitch * 0.6:
                continue
            if not clipped:
                boxes[(column, row)] = (frame_index, int(centre_x), y0, y1)
            tile_jobs.append(image[y0:y1, x0:x1].copy())
            tile_meta.append((column, row, frame_index))

    if not tile_jobs:
        return 0

    produced: dict[int, list[str]] = {}
    for sequence, reads in ocrpool.read_tiles(tile_jobs, _sweep_bands, psm=6):
        produced[sequence] = reads

    # Rebuilt exactly as the serial fan-out used to append them: positions in
    # visiting order, variants in _sweep_bands order — decide_cell carries
    # `learned` forward, so the order the readings are decided in is part of
    # the answer.
    owners: list[tuple[int, int, int]] = []
    texts: dict[int, str] = {}
    for sequence, meta in enumerate(tile_meta):
        for text in produced.get(sequence, []):
            if text:
                texts[len(owners)] = " ".join(text.split())
            owners.append(meta)

    if not owners:
        return 0

    _dump_crops("sweep",
                _band_jobs_for_dump(tile_jobs)
                if os.environ.get("WFT_DEBUG_STATE") else [],
                owners, texts, lambda o: o[:2])

    if os.environ.get("WFT_DEBUG_SWEEP"):
        _raw: dict = {}
        for _i, (_col, _row, _fi) in enumerate(owners):
            _t = texts.get(_i)
            if _t and len(_t) > len(_raw.get((_col, _row), "")):
                _raw[(_col, _row)] = _t
        _seen = {(a, b) for a, b, _ in owners}
        print(f"SWEEP frame {frame_width}x{frame_height}, "
              f"{len(_seen)} position(s), rows "
              f"{min(b for _a, b in _seen)}..{max(b for _a, b in _seen)}",
              flush=True)
        for _key in sorted(_seen, key=lambda k: (k[1], k[0])):
            print(f"  c{_key[0]} r{_key[1]}: "
                  f"{_raw.get(_key, '(nothing read)')[:44]!r}", flush=True)

    # Every decision a position produced, then one chosen among them.
    offers: dict[tuple[int, int], list] = {}
    for index, (column, row, frame_index) in enumerate(owners):
        text = texts.get(index)
        if not text:
            continue
        decision = decide_cell([text], names, ignored, decoy_words, learned)
        if decision.name is None:
            continue
        offers.setdefault((column, row), []).append((decision, frame_index))

    _second_look(offers, boxes, spare_frame, owners, grid, screen_pitch, half,
                 frame_files, scrolls, names, ignored, decoy_words, learned,
                 notes, cache=cache)

    _report_sweep(owners, texts, boxes)

    best: dict[tuple[int, int], tuple] = {}
    for key, options in offers.items():
        chosen = _choose_offer(options)
        # A shorter name must not win against a longer one it is contained in.
        #
        # The variants of one tile do not all read equally: on a light theme
        # only the plain-luma separation produces text at all and the colour
        # ones produce noise, and noise that happens to spell "Blueprint" or
        # "Atlas" matches a catalogue name *exactly* while the correct, longer
        # reading is at 92 because a stroke of the artwork came through as a
        # bracket. Score alone then prefers the wrong one, and it does so
        # silently: "Atlas Prime Blueprint" is recorded as "Atlas Blueprint",
        # which exists, so nothing downstream objects.
        #
        # Word containment rather than prefix: it is the middle word that goes
        # missing. "Atlas Blueprint" sits inside "Atlas Prime Blueprint" and is
        # not a prefix of it.
        for decision, frame_index in options:
            if (decision.name != chosen[0].name
                    and decision.score >= NAME_FLOOR
                    and _contains_words(decision.name, chosen[0].name)):
                chosen = (decision, frame_index)
        # And when one reading of this tile is another with words missing, the
        # one that read more of it is the better witness - whatever they score.
        #
        # The tile that is "Xaku Prime Neuroptics Blueprint" offers both
        # "SIS Neuroptics Blueprint", which scores 93.9 against a Warframe the
        # user owns no part of, and "il Xaku Prime SIS Neuroptics Blueprint",
        # which scores 89.9 against its own name. Score alone takes the first:
        # a truncated reading fits a shorter name better than a complete
        # reading fits the right one, every time. The same tile read twice is
        # not two opinions to be scored against each other - it is one tile,
        # once with the front of its name and once without.
        fuller = [(d, f) for d, f in options
                  if d.score >= NAME_FLOOR
                  and _contains_words(d.reading, chosen[0].reading)]
        if fuller:
            chosen = max(fuller, key=lambda o: len(o[0].reading))
        best[key] = chosen

    # Second look. A tile whose band names something else may simply have been
    # cropped a row out: the mapping from a page row to a screen row is an
    # estimate, and half a row of error rounds the wrong way. Reading the band
    # one row up and one row down settles it - the box that spells the name is
    # the right box, and no other evidence can outrank that.
    retry_jobs: list[bytes] = []
    retry_owners: list[tuple[int, int, int, int, int]] = []
    by_frame_again: dict[int, list] = {}
    for (column, row), (decision, frame_index) in best.items():
        standing = held.get((column, row))
        if standing is None or decision.name == standing.name:
            continue
        by_frame_again.setdefault(frame_index, []).append((column, row))
    for frame_index, keys in by_frame_again.items():
        path = frame_files.get(frame_index)
        image = cache.frame(path) if path else None
        if image is None:
            continue
        height, width = image.shape[:2]
        for column, row in keys:
            # A tile cut by the screen edge was read off what is on screen and
            # has no box (see the sweep above), so there is nothing to shift a
            # row up or down from. Indexing it raised KeyError and ended the
            # whole scan: measured on fixtures/inventory/crop_c.png, whose top
            # row is cut, at 2564 pixels wide and at half that.
            box = boxes.get((column, row))
            if box is None:
                continue
            _frame, centre_x, y0, y1 = box
            for step in (-1, 1):
                shifted0 = int(y0 + step * screen_pitch)
                shifted1 = int(y1 + step * screen_pitch)
                x0, x1 = int(centre_x - half), int(centre_x + half)
                if shifted0 < 0 or shifted1 > height or x0 < 0 or x1 > width:
                    continue
                tile = image[shifted0:shifted1, x0:x1]
                shared = _TileShared(tile)
                for _label, *weights in _SEPARATIONS[:2]:
                    prepared = _name_band_image(tile, weights,
                                                NAME_BAND_TOPS[0],
                                                shared=shared)
                    if prepared is None:
                        continue
                    ok, buffer = cv2.imencode(".png", prepared)
                    if ok:
                        retry_jobs.append(buffer.tobytes())
                        retry_owners.append((column, row, frame_index,
                                             shifted0, shifted1))
    rescued = 0
    if retry_jobs:
        retry_texts: dict[int, str] = {}
        for index, text in ocrpool.read_crops(retry_jobs, psm=6):
            if text:
                retry_texts[index] = " ".join(text.split())
        for index, (column, row, frame_index, shifted0, shifted1) in enumerate(retry_owners):
            text = retry_texts.get(index)
            standing = held.get((column, row))
            if not text or standing is None or standing.verified_box:
                continue
            decision = decide_cell([text], names, ignored, decoy_words, learned)
            if decision.name == standing.name:
                standing.verified_box = (frame_index, boxes[(column, row)][1],
                                         shifted0, shifted1)
                rescued += 1
    if rescued:
        notes.append(f"{rescued} tile(s) whose box was a row out — found by "
                     f"reading the band above and below and keeping the one "
                     f"that spells the name")

    added = challenged = confirmed = restored = 0
    for (column, row), (decision, frame_index) in best.items():
        standing = held.get((column, row))
        if standing is not None:
            if decision.name == standing.name:
                standing.verified_box = boxes.get((column, row))
                confirmed += 1
            elif (decision.score >= NAME_FLOOR
                    and (_contains_words(decision.name, standing.name)
                         or (len(decision.name) > len(standing.name)
                             and _reading_fits_name(decision.name,
                                                    standing.reading)))):
                # The band read the name the page pass had truncated. This one
                # the band wins outright rather than asking, because it is not
                # a disagreement about which item this is - it is the same item
                # with a word restored, and the shorter form only looked right
                # because a shorter item of that name happens to exist.
                #
                # "Atlas Prime Blueprint" was being recorded as "Atlas
                # Blueprint", which is a real item, so nothing objected. Prime
                # parts lose the middle word this way by the dozen.
                ident = lookup[decision.name]
                standing.name = decision.name
                standing.reading = decision.reading
                standing.score = decision.score
                standing.lead = decision.lead
                standing.part_unique = ident.part
                standing.item_unique = ident.item
                standing.relic_unique = ident.relic
                if decision.refinement is not None:
                    standing.refinement = decision.refinement
                standing.doubt = decision.doubt
                standing.doubt_word = decision.doubt_word
                standing.verified_box = boxes.get((column, row))
                restored += 1
            # The band disagrees with the page pass about what this tile is.
            # Not resolved here - which witness is better in general is a
            # measurement, not a guess made in passing - but the band's answer
            # is offered, which is what a truncated name needs: "Yareli" and
            # "Yareli Prime" both match exactly, and every suggestion derived
            # from the shorter one is wrong.
            if (decision.name != standing.name
                    and decision.score >= standing.score
                    and not standing.doubt):
                standing.alternative = decision.name
                standing.doubt_word = None
                standing.doubt = (
                    f'this tile\'s own name reads "{decision.reading}", '
                    f'the page pass read "{standing.reading}" — the picture '
                    f'says which')
                challenged += 1
            continue
        # Two positions one row apart can be the same tile seen either side of
        # a boundary. Same name and same rank in the same column is that, not
        # a second copy - the ranks are what keep Oberon [0] and Oberon [30]
        # apart.
        twin = any(other.column == column and abs(other.row - row) <= 1
                   and other.name == decision.name
                   and other.rank == decision.rank
                   for other in candidates)
        if twin:
            continue
        ident = lookup[decision.name]
        candidates.append(Candidate(
            column=column, row=row, name=decision.name, score=decision.score,
            reading=decision.reading, sightings=1, lead=decision.lead,
            part_unique=ident.part, item_unique=ident.item,
            relic_unique=ident.relic, rank=decision.rank,
            refinement=decision.refinement, doubt=decision.doubt,
            doubt_word=decision.doubt_word,
            # Which capture it came from. Not decoration: two screenshots
            # that overlap show one tile twice, and the merge that removes the
            # copy works by asking how many tiles a single capture claimed.
            # Left empty, every swept tile looked like it came from the same
            # imaginary capture, so a tile in the overlap was kept twice.
            sources=frozenset({frame_source.get(frame_index, 0)}),
            from_still=frame_index in still_frames,
            verified_box=boxes.get((column, row))))
        # Register the position so the passes after this one - pictures, stack
        # badges, the re-read - find it the way they find any other tile.
        cells.setdefault((column, row), []).append(pm.Word(
            text=decision.reading,
            x=grid.column_phase + column * grid.column_pitch,
            virtual_y=(grid.row_phase + row * grid.row_pitch),
            width=1.0, height=1.0,
            confidence=float(cal_mod.MIN_WORD_CONFIDENCE),
            frame_index=frame_index))
        added += 1
    if added:
        notes.append(f"{added} tile(s) found by reading the name band at every "
                     f"grid position, which the page pass had not recognised")
    if challenged:
        notes.append(f"{challenged} tile(s) where the name band and the page "
                     f"pass disagree — sent to review with both readings")
    if restored:
        notes.append(f"{restored} tile(s) had a word restored by their own "
                     f"name band — the page pass had read a shorter name that "
                     f"also exists")
    if confirmed:
        notes.append(f"{confirmed} tile(s) confirmed by their own name band — "
                     f"stack count and picture come from that same box")
    return added


#: Where to start reading for the name, as a fraction of tile height. Two
#: values because one cannot serve both cases, and the difference is not
#: subtle:
#:
#:     "Limbo Prime / Blueprint"   from 0.72: '» ("SR RISAY B | fllllc Blueprint'
#:                                 from 0.66: 'Limbo Prime Blueprint'
#:     "Limbo Helmet"              from 0.72: 'Limbo Helmet'
#:                                 from 0.66: "L.imbo Helmet , ',"
#:
#: Both are real tiles from one scan, and each is unreadable at the other's
#: setting.
#:
#: A wrapped name starts higher, so a band that fits a single line cuts through
#: the top of its first row - which is how "Limbo Prime Blueprint" arrived as
#: "Prim Blueprunt", with the item name gone and only the qualifier left. A
#: band that fits two lines drags extra artwork into the single-line case.
#: Trying the wider one first and falling back costs one extra read on tiles
#: where the first attempt does not improve anything.
#:
#: 0.62 is the third, and it was added because two settings were not enough.
#: "Baruuk Prime / Chassis" sat high enough that both cut its first line in
#: half, and what came back was "me hassis" - the frame name gone, the
#: qualifier mangled, and the six candidates offered in the review were Mesa,
#: Ember, Miter and Mag, none of them the answer. Measured on that tile:
#:
#:     from 0.58: 'Baruuk Prime Chassis'
#:     from 0.62: 'Baruuk Prime Chassis'
#:     from 0.66: 'WCIIUNN l 1AR L Chassis'
#:     from 0.72: 'Chassis'
#:
#: Tiles that already read correctly at 0.66 and 0.72 are unaffected: Inaros,
#: Hildryn, Lavos and Khora all read correctly from 0.58 down as well.
#: Where a band crop starts when nobody says. A fraction, never "found":
#: the callers that pass nothing want the plain one-line case.
NAME_BAND_DEFAULT = 0.66

#: The crops tried per tile. "found" cuts at the printed name itself;
#: the fractions are the fallback for tiles where it cannot be located.
NAME_BAND_TOPS = ("found", 0.66, 0.72, 0.62, 0.55, 0.48)

#: The crops of the second look for a tile whose reading does not carry the
#: first word of its name. Each is read narrowed to this tile's own
#: name, without the ends of the neighbours' names that stand in a tile cut
#: wider than itself.
NAME_BAND_OWN_TOPS = NAME_BAND_TOPS

#: Where the second look's variants start counting, past the ordinary ones.
OWN_RANK = 1000

#: How much to enlarge the name band before reading it. Same figure the badge
#: reader uses, and for the same reason: Tesseract is trained on document-sized
#: text and a tile name is not.
NAME_BAND_SCALE = 3.0


#: Ways to turn a colour tile into something with the text on one side of a
#: threshold. Each is (name, blue weight, green weight, red weight); the result
#: is that weighted sum, clipped.
#:
#: The interface has themes, so the text is not reliably any one colour. Rather
#: than encode the default one, the scan tries these on a handful of tiles it
#: already read correctly and keeps whichever recovers those readings best -
#: the answer is known there, so picking is measuring rather than guessing.
_SEPARATIONS = (
    ("blue-minus-red", 1.0, 0.0, -1.0),    # pale blue text, the stock theme
    ("red-minus-blue", -1.0, 0.0, 1.0),    # warm text on a cool background
    ("green-minus-red", 0.0, 1.0, -1.0),   # green text
    ("luma", 0.114, 0.587, 0.299),         # plain brightness, no colour at all
    # Two that are not channel combinations at all, for the tiles the three
    # colour ones cannot see: a blueprint already in the foundry is drawn
    # dimmed, light grey letters on a slightly lighter grey plate. Grey means
    # blue equals red, so every difference clips to nothing and Otsu is left
    # thresholding noise.
    ("mask", "mask"),          # the name mask, which has solved this already
    ("levelled", "levelled"),  # luma divided by its own blur, then Otsu
)


def _levelled(band):
    """Luma with the background flattened out, for low-contrast text.

    Dividing an image by a heavily blurred copy of itself removes whatever
    gradient the plate has and leaves the letters standing. The standard
    remedy for text that is only a few levels away from what it sits on.
    """
    import cv2
    import numpy as np

    grey = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY).astype(np.float32)
    ground = cv2.GaussianBlur(grey, (0, 0), max(3, band.shape[0] / 4))
    flat = np.clip(grey / np.maximum(ground, 1.0) * 128.0, 0, 255).astype(np.uint8)
    _level, mask = cv2.threshold(flat, 0, 255,
                                 cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if mask.mean() <= 127:
        mask = 255 - mask
    return cv2.resize(mask, None, fx=NAME_BAND_SCALE, fy=NAME_BAND_SCALE,
                      interpolation=cv2.INTER_CUBIC)


def _separate(band, weights):
    """One channel combination, thresholded and enlarged for Tesseract."""
    import cv2
    import numpy as np

    blue, green, red = (band[:, :, i].astype(np.float32) for i in range(3))
    combined = weights[0] * blue + weights[1] * green + weights[2] * red
    combined = np.clip(combined, 0, 255).astype(np.uint8)
    _level, mask = cv2.threshold(combined, 0, 255,
                                 cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Tesseract wants dark text on light ground, and which side of the
    # threshold the text landed on depends on the interface theme. On a dark
    # theme the name is the bright minority and has to be flipped; on a light
    # one - Orokin, say - the *ground* is bright and flipping would hand
    # Tesseract white letters on black, which it cannot read.
    #
    # Letters are the minority of a name band either way, so the majority
    # class is the ground. That settles it without knowing the theme.
    if mask.mean() <= 127:
        mask = 255 - mask
    return cv2.resize(mask, None, fx=NAME_BAND_SCALE, fy=NAME_BAND_SCALE,
                      interpolation=cv2.INTER_CUBIC)


class _TileShared:
    """The per-tile work every band variant used to repeat.

    `name_block` and `_name_mask` depend only on the tile, yet the variant
    fan-out asked for them once per separation and once per band top — six
    times each per tile, ~21 ms of the ~133 ms a position costs to prepare.
    Lazy rather than eager, so the retry path — two separations, no mask —
    keeps paying for exactly what it uses.
    """

    __slots__ = ("_tile", "_block", "_ink", "_has_block", "_has_ink",
                 "_columns", "_has_columns")

    def __init__(self, tile):
        self._tile = tile
        self._block = self._ink = self._columns = None
        self._has_block = self._has_ink = self._has_columns = False

    def block(self):
        if not self._has_block:
            self._block = quantity_mod.name_block(self._tile)
            self._has_block = True
        return self._block

    def ink(self):
        if not self._has_ink:
            self._ink = quantity_mod._name_mask(self._tile)
            self._has_ink = True
        return self._ink

    def columns(self):
        if not self._has_columns:
            block = self.block()
            self._columns = (None if block is None else
                             quantity_mod.name_columns(self._tile, block))
            self._has_columns = True
        return self._columns


def _name_band_image(tile, weights, top=None, shared=None, own=False):
    """The name band of a tile, separated by colour and enlarged for reading.

    `top` is a fraction of the tile's height, or the string "found" to cut at
    the printed name itself rather than at a guess about where it is.
    `shared` carries the per-tile products when the caller is fanning out over
    many variants of one tile; without it each call computes its own.

    Returns None when the band is too small to hold anything.
    """
    height = tile.shape[0]
    if top == "found":
        block = (shared.block() if shared is not None
                 else quantity_mod.name_block(tile))
        if block is None:
            return None
        # A little air above and below: thresholding eats the extremes of a
        # glyph, and Tesseract reads a line with margins better than one
        # clipped to its own ink.
        start = max(0, block[0] - int(height * 0.02))
        band = tile[start:min(height, block[1] + int(height * 0.03)), :]
    else:
        band = tile[int(height * (NAME_BAND_DEFAULT if top is None else top)):, :]
    if band.size == 0 or band.shape[0] < 10:
        return None
    left, right = 0, tile.shape[1]
    if own:
        # Only this tile's own name, found from the same block as "found".
        if shared is not None:
            columns = shared.columns()
        else:
            block = quantity_mod.name_block(tile)
            columns = (quantity_mod.name_columns(tile, block)
                       if block is not None else None)
        if columns is None:
            return None
        left, right = columns
        band = band[:, left:right]
    if weights and weights[0] == "levelled":
        return _levelled(band)
    if weights and weights[0] == "mask":
        # The mask from the whole tile, cropped to the band. Computing it on
        # the band alone would decide the polarity from the band, which is
        # mostly letters; the tile has the plate to judge by.
        import cv2

        ink = shared.ink() if shared is not None else quantity_mod._name_mask(tile)
        cut = ink[height - band.shape[0]:, left:right]
        if cut.size == 0:
            return None
        shown = 255 - (cut * 255)
        return cv2.resize(shown, None, fx=NAME_BAND_SCALE, fy=NAME_BAND_SCALE,
                          interpolation=cv2.INTER_CUBIC)
    return _separate(band, weights)


def _sweep_bands(tile):
    """Every prepared band variant of one tile, in bookkeeping order.

    This is the sweep's fan-out — six separations by six band tops — and it
    runs inside a pool worker: `ocrpool.read_tiles` hands the tile over once
    and gets the readings back, so the thresholding, enlarging and encoding
    that used to occupy one core for ~56 seconds per recording (while the
    other fifteen waited for crops) happens where the cores are. Module-level
    because a forked worker receives the function by name.

    The order is a contract: the caller counts the returned readings back
    into per-variant bookkeeping, and `_band_jobs_for_dump` re-creates the
    same sequence for the crop fingerprints.
    """
    prepared_variants = []
    shared = _TileShared(tile)
    for _label, *weights in _SEPARATIONS:
        for band_top in NAME_BAND_TOPS:
            prepared = _name_band_image(tile, weights, band_top,
                                        shared=shared)
            if prepared is not None:
                prepared_variants.append(prepared)
    return prepared_variants


def _band_jobs_for_dump(tiles) -> list[bytes]:
    """The sweep's variants, re-encoded for the crop fingerprint dump.

    The variants are prepared in the pool now and never exist on this side.
    The dump exists to compare two runs of the same input, and preparing
    them again — deterministically, from the same tiles — serves that just
    as well. A full serial prep pass, paid only under WFT_DEBUG_STATE.
    """
    import cv2

    jobs: list[bytes] = []
    for tile in tiles:
        for prepared in _sweep_bands(tile):
            ok, buffer = cv2.imencode(".png", prepared)
            jobs.append(buffer.tobytes() if ok else b"")
    return jobs


def _read_name_band(tile, engine, weights, top=None) -> str:
    """Read the name off a tile crop, separating text from artwork by colour.

    The whole-frame pass reads a tile name as part of a page, and over bright
    artwork it loses letters: "Inaros Prime / Chassis" came back as "Ina
    hassis", both ends present and the middle gone. The middle word is "Prime",
    which is the difference between a set piece and an ordinary one.

    Thresholding the grey image does not fix it and cannot: the text is lighter
    than the background below it and darker than the helmet above it, so no
    single threshold separates them. Colour does. In the stock theme the name
    is pale blue against gold artwork, and blue-minus-red is bimodal where
    brightness is not - Otsu on *that* finds the text. Which combination to use
    is chosen per scan rather than assumed, because the game has themes.

    Measured on three tiles that had lost the word: all three read back
    correctly, "Inaros Prime Chassis" among them.
    """
    height = tile.shape[0]
    band = tile[int(height * (NAME_BAND_DEFAULT if top is None else top)):, :]
    if band.size == 0 or band.shape[0] < 10:
        return ""

    from wft.ocr import engine as engine_mod

    reading = engine.read(_separate(band, weights), psm=engine_mod.PSM_BLOCK)
    return " ".join(reading.text.split())


def _resolve_relic_refinements(candidates, connection, notes) -> int:
    """Point every relic candidate at the identifier for its own refinement.

    Done in one pass at the end rather than wherever a candidate is built,
    because the re-read can change both the name and the refinement after the
    fact, and two places doing this would disagree.

    It matters because a refinement is a separate stack: the inventory shows
    "Meso L3 Relic" and "Meso L3 Relic [Radiant]" as two tiles with two counts.
    Booking both against one identifier would keep whichever was written last
    and throw the other count away, and would then weigh a Radiant relic at
    Intact odds - a fifth of the truth on a rare drop.
    """
    refined = 0
    for candidate in candidates:
        if candidate.relic_unique is None:
            continue
        resolved = dbq.relic_unique_for(
            connection, candidate.name, candidate.refinement)
        if resolved and resolved != candidate.relic_unique:
            candidate.relic_unique = resolved
        if candidate.refinement and candidate.refinement != "INTACT":
            refined += 1
    if refined:
        notes.append(f"{refined} relic tile(s) carried a refinement")
    return refined


def _merge_overlapping_stills(candidates, notes) -> int:
    """Drop tiles that are the same tile photographed twice.

    Every still picture is its own page of the virtual sheet, because two
    screenshots need not overlap and inventing a scroll offset between them is
    worse than not stitching. The cost is that two screenshots which *do*
    overlap show the same tile as two tiles, and nothing downstream can tell
    that from two copies of the item: a player with a single Nikana was
    asked whether it was really two, and the apply table offered to record it twice.

    A capture is a complete view of what it covers. So within one capture,
    tiles are copies and are counted; across captures, the same name is the
    same tile seen again. Only stills are merged - a recording's frames are
    already one page and its overlaps were resolved by the grid.
    """
    from collections import defaultdict

    by_name = defaultdict(list)
    for candidate in candidates:
        if candidate.from_still and candidate.name:
            by_name[candidate.name].append(candidate)

    dropped = []
    for group in by_name.values():
        if len(group) < 2:
            continue
        # One per capture is what a capture can honestly claim. Keep the best
        # reading from each, then keep only as many as the busiest capture saw.
        per_source = defaultdict(list)
        for candidate in group:
            per_source[min(candidate.sources) if candidate.sources else -1].append(candidate)
        keep_count = max(len(members) for members in per_source.values())
        ranked = sorted(group, key=lambda c: (-c.score, -c.sightings))
        dropped.extend(ranked[keep_count:])

    if dropped:
        remove = {id(c) for c in dropped}
        candidates[:] = [c for c in candidates if id(c) not in remove]
        notes.append(f"{len(dropped)} tile(s) were the same tile in two "
                     f"overlapping screenshots, not two of the item")
    return len(dropped)


def _one_box_two_readings(first, second) -> bool | None:
    """Whether two verified boxes describe one tile. None when incomparable.

    A box is (frame, centre x, top, bottom) - the rectangle whose own name
    band was read as this tile's name. Two boxes in *different* frames say
    nothing about each other: a tile appears in every frame it scrolls
    through, so the same tile has a different box in each.

    Within one frame the answer is arithmetic. One tile cannot stand in two
    places of one picture, so boxes that neither overlap vertically nor sit
    near each other horizontally belong to two tiles. The band height doubles
    as the scale, which keeps the comparison free of any pixel constant: it is
    a row's worth of frame, whatever the recording's resolution.
    """
    left, right = first.verified_box, second.verified_box
    if left is None or right is None or left[0] != right[0]:
        return None
    _frame, left_x, left_top, left_bottom = left
    _frame, right_x, right_top, right_bottom = right
    span = max(left_bottom - left_top, right_bottom - right_top)
    if span <= 0:
        return None
    overlap = min(left_bottom, right_bottom) - max(left_top, right_top)
    return overlap * 2 > span and abs(left_x - right_x) * 2 < span


def _same_tile(first, second) -> bool:
    """Whether two candidates for one relic are one tile filed twice.

    The boxes decide when they can. When they cannot, two things still argue
    for one tile: readings from *different* captures, because two overlapping
    screenshots legitimately file one tile under two grid positions; and the
    same column, because the scroll estimate drifts vertically and only
    vertically - the docstring below records the measured case as "one page
    row early on and the next row later", which is the same column throughout.
    """
    verdict = _one_box_two_readings(first, second)
    if verdict is not None:
        return verdict
    if first.sources != second.sources:
        return True
    return first.column == second.column


def _distinct_tiles(group: list) -> list[list]:
    """Split candidates for one relic into the physical tiles they describe."""
    tiles: list[list] = []
    for candidate in group:
        for members in tiles:
            if all(_same_tile(candidate, other) for other in members):
                members.append(candidate)
                break
        else:
            tiles.append([candidate])
    return tiles


def _one_tile_per_relic(candidates, notes) -> int:
    """Two tiles of the same relic are one tile read twice - unless they are not.

    Relics stack: the game draws one tile per relic and refinement with a
    count on it, so a second tile of "Lith A10 Relic [Intact]" is not a second
    holding, it is the same tile filed twice. That happens when the scroll
    estimate drifts past half a row inside one capture - the tile lands under
    one page row early on and the next row later, and the two copies end up
    carrying a count each, one of them the neighbour's.

    Measured on real captures: Lith A10 and Lith K12 both came back
    twice, carrying 3 and 13 between them, where 3 belongs to A10 and 13 to
    K12.

    The copy with the better evidence wins - a box confirmed by its own name
    band first, then the higher score. This is the same argument that keeps a
    built Warframe from being counted twice, applied to the one other place
    where the game guarantees uniqueness.

    **The case the argument does not cover.** Two tiles can claim one relic
    because one of them was misread, and then dropping the weaker throws away
    a holding and files the survivor's count under the wrong name. Measured on
    a relics capture: a tile of nine Requiem I and a tile of twenty
    Requiem III, the latter read as "Requiem I Relic" at a flat 100 because
    the font's numerals merge at this size. The weaker was dropped, so nine
    Requiem I vanished and twenty Requiem III were recorded as Requiem I -
    two wrong numbers and nothing on screen to say so.

    Reading it right is not available: seven of that tile's readings agreed on
    the wrong answer, and the rule that would separate them - more strokes
    wins - was measured against the catalogue and drags 170 name groups
    (Axi A1 against Axi A11, and its kind) towards the longer code.

    So the pictures decide instead of the scores. Where they prove two tiles,
    both are asked about rather than one being dropped: the user sees the two
    crops side by side and settles in a second what no threshold can. Both,
    not just the weaker - here the misread tile is the one scoring 100, and
    asking only about the loser would ask about the wrong tile.
    """
    from collections import defaultdict

    by_relic = defaultdict(list)
    for candidate in candidates:
        if candidate.relic_unique:
            by_relic[(candidate.relic_unique,
                      candidate.refinement or "")].append(candidate)

    dropped = []
    contested = 0
    for group in by_relic.values():
        if len(group) < 2:
            continue
        keepers = []
        for members in _distinct_tiles(group):
            # No quantity in the sort: this pass runs before the stack badges
            # are read, so every count here is still None and the tie-break
            # that used to sit here compared zero against zero. It would not
            # have helped anyway - in the drift case the two copies carry 3
            # and 13 and the larger is the neighbour's.
            ranked = sorted(members, key=lambda c: (bool(c.verified_box),
                                                    c.score), reverse=True)
            keepers.append(ranked[0])
            dropped.extend(ranked[1:])
        if len(keepers) > 1:
            for candidate in keepers:
                candidate.contested = True
                candidate.doubt_word = None
                candidate.doubt = (
                    f"{len(keepers)} tiles claim to be this relic and they "
                    f"cannot all be right — the pictures say which")
            contested += len(keepers)

    if dropped:
        remove = {id(c) for c in dropped}
        candidates[:] = [c for c in candidates if id(c) not in remove]
        notes.append(f"{len(dropped)} relic tile(s) were the same relic filed "
                     f"twice — relics stack, so the game draws one tile each")
    if contested:
        notes.append(f"{contested} relic tile(s) claim relics another tile "
                     f"already claims — asked about rather than dropped, "
                     f"because dropping one loses a holding and misfiles the "
                     f"other")
    return len(dropped)


def _flag_repeated_items(candidates, notes) -> int:
    """Send repeats of a built item to the review instead of accepting them.

    A built Warframe or weapon is one object. Two tiles resolving to the same
    one is possible - a second copy can be built - but it is far more often a
    cosmetic whose name collapses onto the frame's: searching the game for
    "valkyr" returns a Heirloom Helmet, Sigil, Signa and Skin, a plain Helmet,
    and exactly one Valkyr. The scan counted three.

    The decoy list is supposed to catch those and mostly does, but it works on
    whole readings: a tile read as just "Valkyr" matches the frame exactly, and
    an exact match beats any decoy. Nothing downstream then questions it,
    because each tile on its own looks perfect.

    Seeing them together is what gives it away, so the check runs over the
    finished candidates rather than inside the per-tile decision. The strongest
    reading keeps its confidence; the rest go to the review, where the picture
    shows a helmet and the answer takes a second. Parts and relics are exempt:
    holding several of those is ordinary.
    """
    from collections import defaultdict

    by_item = defaultdict(list)
    for candidate in candidates:
        if candidate.item_unique and not candidate.part_unique:
            by_item[candidate.item_unique].append(candidate)

    flagged = 0
    second_copies = 0
    for repeats in by_item.values():
        if len(repeats) < 2:
            continue
        repeats.sort(key=lambda c: (-c.score, -c.lead))
        keeper = repeats[0]
        for candidate in repeats[1:]:
            # A different rank is proof of a different object. A cosmetic
            # carries no rank at all and two readings of one tile carry the
            # same one, so two ranks that differ can only be two copies -
            # measured on a real shelf, "Oberon [0]" beside
            # "Oberon [30]" and "Gotva Prime [30]" beside "Gotva Prime [0]".
            # Both were being asked about as if they were mistakes, and the
            # answer offered first was "ignore", which would have lost them.
            if (candidate.rank is not None and keeper.rank is not None
                    and candidate.rank != keeper.rank):
                second_copies += 1
                continue
            candidate.repeat_of = keeper.name
            flagged += 1

    if flagged:
        notes.append(f"{flagged} repeat(s) of a built item sent to review — "
                     f"usually a cosmetic reading as the frame it belongs to")
    if second_copies:
        notes.append(f"{second_copies} second cop(y/ies) accepted — same item, "
                     f"different rank on the tile, so a different object")
    return flagged


#: How wide a kept frame is stored. A 4K frame holds tiles about 300px across,
#: so at this width a tile is still ~120px - enough to see what is around it,
#: which is the whole point of showing the frame rather than the crop. Full
#: size would be 6MB a frame; this is around 200KB.
CONTEXT_WIDTH = 1600


def _keep_context_frames(candidates, frame_files, cache=None) -> dict:
    """Shrink and encode the frames candidates cite, and rescale their boxes.

    Deduplicated by frame index: a shelf-full of tiles is read off a handful
    of frames, so this is tens of pictures however many candidates there are.
    Candidates whose frame cannot be read lose their box rather than keeping
    one that points into a picture nobody has.
    """
    import cv2

    if cache is None:
        cache = _FrameCache()
    wanted = {c.frame_index for c in candidates if c.frame_index is not None}
    kept: dict[int, bytes] = {}
    scales: dict[int, float] = {}
    for index in sorted(wanted):
        path = frame_files.get(index)
        if not path:
            continue
        image = cache.frame(path)
        if image is None:
            continue
        scale = min(1.0, CONTEXT_WIDTH / image.shape[1])
        if scale < 1.0:
            image = cv2.resize(image, None, fx=scale, fy=scale,
                               interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(".jpg", image,
                                  [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            continue
        kept[index] = buffer.tobytes()
        scales[index] = scale

    for candidate in candidates:
        if candidate.frame_index is None:
            continue
        scale = scales.get(candidate.frame_index)
        if scale is None:
            candidate.frame_index = None
            candidate.bbox = None
        elif candidate.bbox and scale < 1.0:
            candidate.bbox = tuple(int(v * scale) for v in candidate.bbox)
    return kept


def _attach_evidence(candidates, cells, grid, frame_files, scrolls,
                     lookup=None, ignored=None, decoy_words=None,
                     learned=None, progress=None, drift=None, cache=None):
    """Keep a picture of every candidate that needs reviewing, and re-read it.

    The re-read turns some of the review queue into exact matches that never
    reach the review at all. It can only improve a candidate: every new reading
    goes through `decide_cell`, so decoy, fragment and learned-dismissal rules
    all apply, and the result is kept only when it beats what was there.

    Reading happens in the pool. Each attempt is a tesseract process start, and
    doing them one after another on this thread took six minutes on one
    machine where it takes twelve seconds on another - the cost of starting a process
    is not the same everywhere, and a design that pays it per item is only ever
    accidentally fast. Every variant of every tile is prepared up front and read
    at once; trying more variants than strictly needed is cheaper than doing
    fewer of them in sequence.
    """
    import cv2

    can_reread = lookup is not None
    names = list(lookup) if lookup else []

    # One entry per (tile, separation, band height). Collected first, read
    # together, then resolved in priority order per tile.
    jobs: list[bytes] = []
    owners: list = []
    variants: list[int] = []

    def keep(candidate, tile):
        # A picture for every tile, so the review can show what it is talking
        # about even for readings the scan was sure of — "I want to see every
        # tile" is the whole reason to trust the result. Scaled down first: at
        # full size 200 crops are 50MB of PNG held in memory for a thumbnail
        # and a panel, and 320px is more than either needs.
        if candidate.evidence is None:
            shown = tile
            if tile.shape[1] > EVIDENCE_WIDTH:
                scale = EVIDENCE_WIDTH / tile.shape[1]
                shown = cv2.resize(tile, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_AREA)
            ok, buffer = cv2.imencode(".png", shown)
            if ok:
                candidate.evidence = buffer.tobytes()
        # Re-reading is the expensive half and only the doubtful need it. The
        # gate belongs here rather than in `wanted`: this queues twelve OCR
        # jobs per tile, so widening the visit without it turns 144 jobs into
        # two and a half thousand.
        if not can_reread or candidate.confident:
            return
        shared = _TileShared(tile)
        for rank, (index, top) in enumerate(
                (sep, top) for top in NAME_BAND_TOPS
                for sep in range(len(_SEPARATIONS))):
            _label, *weights = _SEPARATIONS[index]
            prepared = _name_band_image(tile, weights, top, shared=shared)
            if prepared is None:
                continue
            ok, buffer = cv2.imencode(".png", prepared)
            if ok:
                jobs.append(buffer.tobytes())
                owners.append(candidate)
                variants.append(rank)
        # The second look for a tile whose reading lost the first word
        # of its name: the same crops, narrowed to the tile's own name.
        if candidate.doubt_word:
            for rank, (index, top) in enumerate(
                    (sep, top) for top in NAME_BAND_OWN_TOPS
                    for sep in range(len(_SEPARATIONS))):
                _label, *weights = _SEPARATIONS[index]
                prepared = _name_band_image(tile, weights, top, shared=shared,
                                            own=True)
                if prepared is None:
                    continue
                ok, buffer = cv2.imencode(".png", prepared)
                if ok:
                    jobs.append(buffer.tobytes())
                    owners.append(candidate)
                    variants.append(OWN_RANK + rank)

    # Every candidate, not only the doubtful ones. The crop is a slice of a
    # frame that is being read anyway; the cost that mattered was the OCR, and
    # that is gated above.
    wanted = {(c.column, c.row): c for c in candidates}
    _visit_tiles(wanted, cells, grid, frame_files, scrolls, keep, drift=drift,
                 cache=cache)

    if not jobs:
        return 0, 0

    # Reported, at last. `scan` gives this pass its own stage so the bar does
    # not sit on the previous one "looking exactly like a hang" - and then
    # this function took the callback and never called it, so the stage
    # never appeared. A scan that failed in the pass after this one was
    # reported by the GUI as a failure of the sweep, because the sweep was
    # the last stage that had said anything.
    if progress:
        progress(0)
    texts: dict[int, str] = {}
    # One tile is done when any of its variants is back; the crops return
    # unordered, so this is the count that climbs steadily.
    tiles_seen: set[int] = set()
    # PSM 6: a name band can wrap to three lines.
    for index, text in ocrpool.read_crops(jobs, psm=6):
        if text:
            texts[index] = " ".join(text.split())
        if progress:
            tiles_seen.add(id(owners[index]))
            progress(len(tiles_seen))

    # Best result per tile, preferring earlier variants on equal footing.
    improved = 0
    dropped: list = []
    by_owner: dict[int, list[int]] = {}
    for index, candidate in enumerate(owners):
        by_owner.setdefault(id(candidate), []).append(index)

    for candidate in {id(o): o for o in owners}.values():
        best = None
        dismissal = None
        own_decisions = []
        for index in sorted(by_owner[id(candidate)], key=lambda i: variants[i]):
            text = texts.get(index)
            if not text:
                continue
            decision = decide_cell([text], names, ignored or [],
                                   decoy_words or set(), learned)
            if variants[index] >= OWN_RANK:
                # The second look is kept apart: it may only answer the doubt
                # the ordinary re-read left standing.
                if decision.name is not None:
                    own_decisions.append(decision)
                continue
            if decision.name is not None:
                if best is None or decision.score > best.score:
                    best = decision
            elif (dismissal is None and decision.dismissed_as
                    and len(text) >= len(candidate.reading) + 4):
                dismissal = decision

        # A re-read that saw less of the name than the standing reading did is
        # not an improvement, whatever it scores. The re-read crops the band at
        # six heights and six separations, and the crops that cut the first
        # word off score *better* than the ones that keep it - a truncated
        # reading fits a shorter name exactly. Three tiles were decided
        # correctly by the band and then overwritten here:
        # "Xaku Prime Neuroptics Blueprint" at 89.9 by "Wisp Neuroptics
        # Blueprint" at 93.9, "Caliban Prime Blueprint" by "Uriel Blueprint",
        # "Perigale Prime Blueprint" by "Aklex Prime Blueprint" - each time by
        # a variant whose reading is the standing one with the front missing.
        #
        # A flag rather than clearing `best`: further down, `best is None`
        # means the re-read named nothing tracked, and combined with a
        # dismissal that discards the tile outright. Clearing it here threw
        # "Caliban Prime Blueprint" away entirely - the standing reading was
        # right, one variant read the artwork as a helmet skin, and the tile
        # stopped existing.
        stands = (best is not None
                  and _contains_words(candidate.reading, best.reading))
        if best is not None and not stands and best.score > candidate.score:
            candidate.name = best.name
            candidate.score = best.score
            candidate.lead = best.lead
            candidate.reading = best.reading
            ident = lookup[best.name]
            candidate.part_unique = ident.part
            candidate.item_unique = ident.item
            candidate.relic_unique = ident.relic
            # The doubt belonged to the old reading. A re-read that produced a
            # better one has answered it — keeping it meant a tile re-read into
            # "Pandero Prime Blueprint" at 100 still arrived in the review
            # complaining that the reading did not carry the word "Pandero".
            candidate.doubt = best.doubt
            candidate.doubt_word = best.doubt_word
            # Only when the re-read saw one. The variants crop the band at
            # different heights, so a winning crop can miss the bracketed line
            # that the page pass caught - and overwriting with None would send
            # a Radiant stack back to Intact, which is half of the bug this
            # was built to fix.
            if best.refinement is not None:
                candidate.refinement = best.refinement
            improved += 1
        elif (best is not None and not stands and best.name != candidate.name
                and best.score >= candidate.score and not candidate.doubt):
            # Same score, different item. Reached on a real capture:
            # a tile pictured as "Cobra & Crane" was recorded as "Thornbak",
            # the name of the tile directly above it, and nothing said so -
            # the page reading matched Thornbak exactly, so the re-read of
            # this tile's own band could not beat it and was thrown away.
            #
            # Not resolved here. Preferring the re-read outright would be a
            # guess about which witness is better in general, and that has to
            # be measured before it is shipped. Preferring neither is what the
            # review is for: the picture is on the card and the answer takes a
            # second.
            candidate.alternative = best.name
            candidate.doubt_word = None
            candidate.doubt = (
                f'this tile\'s own name reads "{best.reading}", '
                f'the page pass read "{candidate.reading}" — the picture says '
                f'which')
        elif best is None and dismissal is not None:
            # The re-read positively named something untracked - a resource, a
            # skin, a key - which is grounds for discarding the tile. Failing to
            # match anything is not: a relic re-read as "Lith 04 Relic" matches
            # no relic because 04 is a misread, and it is still a relic.
            dropped.append(candidate)

        # The second look. The tile's reading lost the first word of
        # its name and the ordinary re-read could not bring it back; the same
        # bands narrowed to the tile's own name may have no neighbour's word
        # in them. Taken only when it names the same item and the doubt is
        # gone - anything else leaves the first decision as it was.
        own_best = max((d for d in own_decisions
                        if d.name == candidate.name and d.doubt is None),
                       key=lambda d: d.score, default=None)
        if (own_best is not None and candidate.doubt_word
                and candidate not in dropped
                and own_best.score >= candidate.score):
            candidate.score = own_best.score
            candidate.lead = own_best.lead
            candidate.reading = own_best.reading
            candidate.doubt = None
            candidate.doubt_word = None
            improved += 1

    for candidate in dropped:
        try:
            candidates.remove(candidate)
        except ValueError:
            pass

    # The picture, from the box that spells the name. Last, so it overrules
    # whatever the frame ranking chose: those two answered to different
    # evidence, and the review showed the difference as a red box around a
    # tile whose name was not the one on the card.
    _picture_from_verified_box(candidates, frame_files, grid, cache=cache)
    return improved, len(dropped)


def _picture_from_verified_box(candidates, frame_files, grid,
                               cache=None) -> int:
    """Point the review's picture at the box the name was read from."""
    import cv2

    if cache is None:
        cache = _FrameCache()
    by_frame: dict[int, list] = {}
    for candidate in candidates:
        if candidate.verified_box:
            by_frame.setdefault(candidate.verified_box[0], []).append(candidate)
    shown = 0
    for frame_index, entries in by_frame.items():
        path = frame_files.get(frame_index)
        image = cache.frame(path) if path else None
        if image is None:
            continue
        height, width = image.shape[:2]
        half = grid.column_pitch * (0.5 + 0.12)
        for candidate in entries:
            _frame, centre_x, y0, y1 = candidate.verified_box
            x0, x1 = max(0, int(centre_x - half)), min(width, int(centre_x + half))
            y0, y1 = max(0, y0), min(height, y1)
            if x1 - x0 < 20 or y1 - y0 < 20:
                continue
            tile = image[y0:y1, x0:x1]
            shown_tile = tile
            if tile.shape[1] > EVIDENCE_WIDTH:
                scale = EVIDENCE_WIDTH / tile.shape[1]
                shown_tile = cv2.resize(tile, None, fx=scale, fy=scale,
                                        interpolation=cv2.INTER_AREA)
            ok, buffer = cv2.imencode(".png", shown_tile)
            if ok:
                candidate.evidence = buffer.tobytes()
                candidate.frame_index = frame_index
                candidate.bbox = (x0, y0, x1, y1)
                shown += 1
    return shown


def _read_quantities(candidates, cells, grid, frame_files, scrolls,
                     progress=None, notes: list | None = None,
                     cache=None) -> int:
    """Read the stack badge on every tile.

    Every tile, not only the stackable ones: which items stack is a fact about
    the game that this code would otherwise have to encode and keep current,
    while the tile itself already says it. No badge drawn means the item does
    not stack, and the reader reports that as `None` rather than guessing one.
    """
    from wft.inventory import quantity as quantity_mod
    from wft.ocr import engine as engine_mod

    if cache is None:
        cache = _FrameCache()
    read = 0
    # Only things that can stack. A built Warframe or weapon is a single object
    # with its own rank - two of them are two tiles, and the game draws no badge
    # on either. Reading one anyway is not harmless: three different Warframes
    # came back as exactly 4, some fixed piece of the tile being read as a digit,
    # and each would have recorded four of a frame the user owns one of.
    #
    # The distinction is already in the data. `_tracked_names` gives parts a
    # part_unique, relics neither, and whole built items an item_unique - and it
    # is exactly the last group that does not stack.
    wanted = {(c.column, c.row): c for c in candidates if not c.item_unique}

    # No padding: the badge position is measured as a fraction of the tile, so
    # a crop wider than the tile puts the region somewhere else entirely.
    #
    # A digit allowlist, which an earlier measurement rejected and a later one
    # earned back. The old objection stands on its own terms: forcing the
    # alphabet to digits removes the evidence that a crop holds no text at all,
    # because noise has to come back as *something*. What changed is that the
    # question "does this crop hold a number" is now answered before the read
    # rather than by it - `has_badge` finds the icon's colour, `shows_tick`
    # finds its shape, and `digit_crops` hands over the figures alone. By then
    # applies, the crop is known to hold a count.
    #
    # Measured on the badge crops from real captures, against counts
    # read off the pictures by eye: without the list 6 of 9, with it 8 of 9,
    # and no wrong answers either way. The list is what turns "2" read as "Z"
    # into "2".
    # Crops are collected first and read together. One tesseract process per
    # tile, run one after another on this thread, is about a second each - fine
    # when the cheap pre-filter thins them out and seven minutes when it does
    # not. The pool turns that into fourteen at a time.
    import cv2

    # Announced before the tiles are visited, not after: the visit is the
    # slow half, and until it had finished the bar still named the stage
    # before this one.
    if progress:
        progress(0)

    collected: list[bytes] = []
    #: Which crops belong to which candidate, in reading order. One list per
    #: sighting, because the same tile is read in more than one frame - and one
    #: entry per figure within it, because the count is read a character at a
    #: time.
    owned_by: dict[int, list[list[int]]] = {}
    owners: dict[int, object] = {}

    def collect(candidate, tile):
        prepared = quantity_mod.badge_crop(tile)
        if prepared is None:
            return
        if os.environ.get("WFT_DEBUG_BADGE_SAVE"):
            # The badge crop at the resolution the scan actually reads. The
            # review's picture is a 320px copy and the fault does not survive
            # the downscale, so a fixture cut from it cannot reproduce this.
            import re
            wanted = os.environ["WFT_DEBUG_BADGE_SAVE"]
            name = candidate.name or ""
            if wanted.lower() in name.lower():
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                cv2.imwrite(os.environ.get("WFT_DEBUG_BADGE_DIR", "/tmp")
                            + f"/badge-{slug}-{len(collected)}.png", prepared)
        indices = []
        for glyph in quantity_mod.digit_crops(prepared):
            ok, buffer = cv2.imencode(".png", glyph)
            if not ok:
                return
            indices.append(len(collected))
            collected.append(buffer.tobytes())
        if indices:
            owned_by.setdefault(id(candidate), []).append(indices)
            owners[id(candidate)] = candidate

    # A tile whose own name band was read gets its badge from that same box,
    # once, with no vote. The vote existed to survive a crop that had landed on
    # the wrong tile; a box that spells the item's name has not. Voting over
    # three guesses is the weaker evidence, and it is what recorded Meso F5's
    # count as its neighbour's 20 - two of the three sightings agreed with each
    # other and with the wrong tile.
    verified = [c for c in wanted.values() if c.verified_box]
    by_frame: dict[int, list] = {}
    for candidate in verified:
        by_frame.setdefault(candidate.verified_box[0], []).append(candidate)
    for frame_index, entries in by_frame.items():
        path = frame_files.get(frame_index)
        image = cache.frame(path) if path else None
        if image is None:
            continue
        height, width = image.shape[:2]
        for candidate in entries:
            _frame, centre_x, y0, y1 = candidate.verified_box
            half = grid.column_pitch / 2
            # Clamped, like the crop the name was read from. The badge sits at
            # the tile's top left, so losing a few pixels off the right edge
            # costs nothing - where skipping the tile costs its whole count,
            # which then defaults to one. That is what the rightmost column
            # did the moment it started being read at all.
            x0, x1 = max(0, int(centre_x - half)), min(width, int(centre_x + half))
            if y0 < 0 or y1 > height or x1 - x0 < grid.column_pitch * 0.6:
                continue
            collect(candidate, image[y0:y1, x0:x1])

    left = {key: candidate for key, candidate in wanted.items()
            if not candidate.verified_box}
    _visit_tiles(left, cells, grid, frame_files, scrolls, collect, cache=cache,
                 pad=0.0, require_full=True, sightings=BADGE_SIGHTINGS)

    # A second look for the verified tiles that came back with nothing. Reading
    # one box once is the right evidence when it works, but a single frame can
    # have a tooltip over the corner or catch the tile mid-scroll, and then a
    # tile that does carry a badge is recorded as one that does not - which is
    # to say, as a single copy. The same scan losing Meso B9 one run and Meso
    # F5 the next, with no other change, is what that looks like.
    #
    # Only those that found nothing: a verified box that produced digits is
    # better evidence than any vote, and this must not overrule it.
    second_chance = {key: candidate for key, candidate in wanted.items()
                     if candidate.verified_box and id(candidate) not in owned_by}
    if second_chance:
        _visit_tiles(second_chance, cells, grid, frame_files, scrolls, collect,
                     pad=0.0, require_full=True, sightings=2, cache=cache)

    # The total is what was collected, not how many candidates there are: item
    # candidates are skipped and a tile with no badge yields no crop, so
    # counting candidates meant the bar for this phase could never reach its
    # own end.
    if progress:
        progress(0, len(collected))
    # PSM 10 is "one character", which is what each crop now holds.
    texts: dict[int, str] = {}
    for index, text in ocrpool.read_crops(
            collected, progress=lambda done: progress and progress(done, len(collected)),
            psm=10, allowlist=quantity_mod.DIGIT_ALLOWLIST):
        texts[index] = text

    read = 0
    disputed = 0
    for key, sightings in owned_by.items():
        votes = [quantity_mod.assemble([texts.get(i, "") for i in indices])
                 for indices in sightings]
        value, agreed = _agreed_count(votes)
        if os.environ.get("WFT_DEBUG_BADGE"):
            # Every sighting's own answer, and the figures it read them from.
            # A count is decided by majority over three frames, so a wrong one
            # is not visible in the tile the review shows - that is one of the
            # three, and it can be the one that was right.
            wanted_name = os.environ["WFT_DEBUG_BADGE"]
            name = getattr(owners[key], "name", "") or ""
            if wanted_name == "all" or wanted_name.lower() in name.lower():
                figures = [[texts.get(i, "") for i in indices]
                           for indices in sightings]
                print(f"BADGE {name}: votes={votes} -> {value} "
                      f"(unanimous={agreed}) figures={figures}", flush=True)
        if value is not None:
            owners[key].quantity = value
            read += 1
            if not agreed:
                disputed += 1
                owners[key].doubt_word = None
                owners[key].doubt = (
                    f"the stack count read differently in different frames "
                    f"({', '.join(str(v) for v in sorted(set(v for v in votes if v)))})"
                    f" — {value} is what most of them said")
    if disputed and notes is not None:
        notes.append(f"{disputed} stack count(s) disagreed between frames — "
                     f"the majority was taken and the tile flagged")
    return read


#: How many frames a badge is read in. One frame is a guess: the most central
#: sighting of a tile can still be the one with the search bar drawn over it,
#: and a badge read through it comes back as something plausible rather than as
#: nothing. Measured on real captures, a stack of 25 became 1 that way.
#: Three because the cost is per figure and small - about 200 extra reads on a
#: scan that already does thousands - and because two cannot break a tie.
BADGE_SIGHTINGS = 3


def _agreed_count(votes) -> tuple[int | None, bool]:
    """The count the frames agree on, and whether they were unanimous.

    Blanks do not vote. A frame where the badge was unreadable says nothing
    about the number; counting it as a "no" would let one occluded frame veto
    two clear ones.
    """
    seen = [value for value in votes if value is not None]
    if not seen:
        return None, True
    tally: dict[int, int] = {}
    for value in seen:
        tally[value] = tally.get(value, 0) + 1
    best = max(tally.items(), key=lambda pair: (pair[1], -pair[0]))
    return best[0], len(tally) == 1


class Ident(NamedTuple):
    """What a name resolves to. Exactly one of these is set.

    A named tuple rather than a bare one: this started as a pair, gained a
    third field for relics, and the widget that unpacked two of three fields
    failed silently inside a Qt slot - which looked like the scan hanging. Any
    future fourth kind of thing adds a field here and breaks nothing.
    """

    part: str | None = None
    item: str | None = None
    relic: str | None = None


@dataclass
class Decision:
    """What one cell resolved to, or why it did not."""

    name: str | None
    score: float
    lead: float
    reading: str
    #: The rank shown on the tile, when one was legible. Captured before
    #: `normalise` strips it: the rank is the only evidence in the picture that
    #: an item has been levelled, and levelling to 30 is what grants mastery.
    rank: int | None = None
    #: The refinement printed after a relic's name, when one was legible. Two
    #: refinements of the same relic are two stacks with two counts and two
    #: sets of odds, so this is what tells them apart.
    refinement: str | None = None
    #: Why this cannot be accepted without asking, when it cannot. A field of
    #: its own rather than a doctored score: the first version of this clamped
    #: `lead` to force the same outcome, which then made the review say "several
    #: names fit this reading equally well" about a reading where one name fit
    #: and the doubt was elsewhere.
    doubt: str | None = None
    #: See `Candidate.doubt_word`.
    doubt_word: str | None = None
    #: The untracked name that explained the reading, when one did. "No tracked
    #: match" and "this is positively a resource" are different findings, and
    #: only the second is grounds for discarding a tile outright.
    dismissed_as: str | None = None


#: How well the matched name's first word has to show up in the reading. Below
#: this the reading has lost the identifying part of the name. Measured over
#: real readings: everything that should have been asked about scored 90.9 or
#: less, everything that was rightly accepted scored 92.3 or more.
HEAD_MATCH_MIN = 92.0


def _head_score(reading: str, name: str | None) -> float:
    """How well the name's first word appears anywhere in the reading.

    Anywhere rather than at the front, because OCR routinely puts rubbish
    before the text - "| G LGE Daikyu Prime Blueprint" is a good reading of
    Daikyu Prime Blueprint and its first token is a stray letter.
    """
    from rapidfuzz import fuzz

    if not name:
        return 100.0
    head = name.split()[0]
    words = reading.split() or [reading]
    return max(fuzz.ratio(head, word, processor=str.casefold) for word in words)


def _has_prime_twin(name: str | None, names: list[str]) -> bool:
    """Whether a Prime version of this name is also tracked.

    Built by removing " Prime" from every name that has it, which handles the
    cases a positional rule does not: "Dual Zoren Prime" is "Dual Zoren", not
    "Dual Prime Zoren".
    """
    if not name or "prime" in name.casefold():
        return False
    global _PRIME_TWINS
    if _PRIME_TWINS is None or _PRIME_TWINS[0] is not names:
        stripped = {n.replace(" Prime", "", 1) for n in names if " Prime" in n}
        _PRIME_TWINS = (names, stripped)
    return name in _PRIME_TWINS[1]


#: Cached per name list, which is built once per scan and reused for every cell.
_PRIME_TWINS: tuple[list[str], set[str]] | None = None


def _folded_views(names: list[str], ignored: list[str]):
    """Case-folded copies of both name lists, built once and reused.

    `decide_cell` runs a few thousand times per scan, and rapidfuzz's
    `processor=str.casefold` re-folds every candidate on every call - for the
    seventeen thousand decoy names that alone is most of the call's cost.
    Folding once here and handing rapidfuzz pre-folded strings computes the
    same scores; the indices map each winner back to its original casing.

    The confusion keys are folded here for the same reason. The rescue below
    searches the whole tracked list, and `processor=match_mod.ocr_key` would
    rebuild every key on every call - the cost the casefolding was pulled out
    to avoid, paid a second time.
    """
    global _FOLDED
    if (_FOLDED is None or _FOLDED[0] is not names or _FOLDED[1] is not ignored):
        _FOLDED = (names, ignored,
                   [name.casefold() for name in names],
                   {name.casefold(): name for name in names},
                   [name.casefold() for name in ignored],
                   [match_mod.ocr_key(name) for name in names])
    return _FOLDED[2], _FOLDED[3], _FOLDED[4], _FOLDED[5]


#: Cached like _PRIME_TWINS, and for the same reason: the lists are built once
#: per scan and never mutated, so identity is the cheap freshness test.
_FOLDED: tuple | None = None


def decide_cell(hypotheses, names: list[str], ignored: list[str],
                decoy_words: set[str], learned: list[str] | None = None) -> Decision:
    """Resolve one cell's readings to a tracked name, or to nothing.

    Pulled out of `scan` so the fixture test can measure this rather than its
    own copy of it. The copy had drifted: it matched case-sensitively long
    after that was fixed here, and knew nothing about leads, decoys or
    fragments - so every change of the last few rounds was invisible to the
    number that was supposed to be watching them.
    """
    best: tuple[str, float, str] | None = None
    best_lead = 0.0

    names_folded, exact, ignored_folded, names_keyed = _folded_views(
        names, ignored)

    seen_rank: int | None = None
    seen_refinement: str | None = None

    for hypothesis in hypotheses:
        bare, rank = quantity_mod.strip_rank(hypothesis)
        if rank is not None:
            seen_rank = rank if seen_rank is None else max(seen_rank, rank)
        _, refinement = quantity_mod.strip_refinement(bare)
        if refinement is not None and seen_refinement is None:
            seen_refinement = refinement
        text = match_mod.normalise(hypothesis)
        if not text or _is_fragment(text):
            continue

        if len(text) < MIN_READING:
            # Short readings are mostly noise, and noise this short scores well
            # against something by accident. But eight tracked items have names
            # this short - Ash, Bo, Hek, Lex, Mag, Nyx, Pox, War - and rejecting
            # every short reading made all eight unrecognisable, which is how
            # "Ash [30]" came back as nothing at all.
            #
            # An exact match is the way through: it cannot be an accident, and
            # anything less than exact at this length is not evidence.
            hit = exact.get(text.casefold())
            if hit is None:
                continue
            if best is None or 100.0 > best[1]:
                best, best_lead = (hit, 100.0, text), 100.0
            continue
        # Two results rather than one, so the margin over the runner-up is
        # known. extractOne would only say how well the winner fits, never
        # whether anything else fits just as well.
        # Ten rather than four: relic names differ by one character by
        # construction, so a reading can tie a dozen of them at once and the
        # plausible one need not be in the first four the scorer happens to
        # return. "Lith 04 Relic" ties Z4, V4, N4, W4 and O4 - and O4 was the
        # answer, sitting fifth.
        ranked = [(names[index], score, index) for _, score, index in
                  process.extract(text.casefold(), names_folded,
                                  scorer=fuzz.ratio, limit=10)]
        if not ranked:
            continue

        # Prefer the candidate that differs only in characters OCR trades for
        # one another. This searches the whole list rather than the ten already
        # returned: relic codes differ by one character by construction, so
        # "Lith 04 Relic" ties twenty-three relics at 92.3 and which ten come
        # back is arbitrary - Lith O4, the answer, was not among them.
        #
        # Not only when the raw scores tie, which is what this used to ask.
        # Five Requiem relics differ by a roman numeral, and Tesseract renders
        # "III" as "Ill" about as often as it gets it right. Raw, that reading
        # is *closer* to "Requiem I Relic" (93.8) than to the truth (88.2) -
        # one character wrong is one character wrong, whichever character it
        # is - so there was no tie to break and the wrong relic won outright.
        # Collapsed, the same reading is 100 against "Requiem III Relic" and
        # 93.8 against "Requiem I Relic". Measured on a relics
        # capture: the tile reading "Requiem Il Relic" used to land on Requiem
        # I - already claimed by another tile - and its count of 10 was lost
        # with it. It now stands as Requiem II, which is what it is.
        #
        # Still a comparison between two named candidates rather than a scorer,
        # which is the line `_CONFUSIONS` draws: it fires only when the
        # collapsed reading fits some other name strictly better than it fits
        # the leader, and only when the two are within reach of one another.
        # The raw score is what gets reported either way - handing on the
        # collapsed 100 is exactly the "Bo is 80" claim that table warns
        # against.
        #
        # An exact reading is left alone. Nothing scores above 100, so the
        # search could only confirm the answer already in hand, at the price of
        # a pass over every tracked name for the cleanest tiles on the screen.
        if ranked[0][1] < 100.0:
            key = match_mod.ocr_key(text)
            collapsed = process.extractOne(key, names_keyed, scorer=fuzz.ratio)
            if (collapsed is not None
                    and collapsed[1] > fuzz.ratio(key, names_keyed[ranked[0][2]])
                    and abs(collapsed[1] - ranked[0][1]) < 25):
                rescued = names[collapsed[2]]
                ranked = [(rescued, ranked[0][1], collapsed[2])] + [
                    r for r in ranked if r[0] != rescued]

        found = ranked[0]
        lead = found[1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
        # On a tie the longer reading wins. A tile's hypotheses include both its
        # first line alone and its lines joined, so "Kaszas" and "Kaszas
        # Blueprint" can each score 100 - against the weapon and against its
        # blueprint respectively. Taking whichever was tested first turns
        # blueprints into finished weapons, which is the difference between "you
        # still need this" and "you have it".
        better = best is None or found[1] > best[1] or (
            found[1] == best[1] and len(text) > len(best[2]))
        if better:
            best = (found[0], found[1], text)
            best_lead = lead

    if best is None:
        return Decision(None, 0.0, 0.0, "", rank=seen_rank, refinement=seen_refinement)
    if best[1] < MIN_SCORE:
        # Still ask what it was, even though no tracked name fits. "Limbo
        # Helmet" reaches only 60 against tracked names and 100 against the
        # skin, and the caller wants to know the difference between "nothing
        # matched" and "this is a skin".
        weak = process.extractOne(best[2].casefold(), ignored_folded,
                                  scorer=fuzz.ratio)
        return Decision(None, best[1], best_lead, best[2], rank=seen_rank, refinement=seen_refinement,
                        dismissed_as=ignored[weak[2]] if weak and weak[1] >= MIN_SCORE
                        else None)

    # A reading the user has dismissed before. Compared loosely because OCR
    # does not repeat itself exactly - the same tile comes back as "Chite
    # BlueprInt" one run and "Chite Blueprnt" the next - and only against
    # readings that were not confident anyway, so a learned dismissal can never
    # suppress something the scan is sure of.
    if learned and not _clears_confidence(best[1], best_lead):
        seen = process.extractOne(best[2], learned, scorer=fuzz.ratio,
                                  processor=str.casefold)
        if seen and seen[1] >= LEARNED_MATCH:
            return Decision(None, best[1], best_lead, best[2])

    # If a cosmetic or any other untracked name explains the reading better,
    # this is that thing. Deliberately not a threshold on the tracked score: a
    # skin's name legitimately scores high against the item it belongs to, so
    # the question is which of the two fits better, not whether one fits well.
    dismissal = process.extractOne(best[2].casefold(), ignored_folded,
                                   scorer=fuzz.ratio)
    if dismissal and dismissal[1] > best[1]:
        return Decision(None, best[1], best_lead, best[2], rank=seen_rank, refinement=seen_refinement,
                        dismissed_as=ignored[dismissal[2]])

    # A one-word reading that is a word of some decoy, and that did not match
    # its tracked name exactly, is a fragment of that decoy rather than a poor
    # reading of a tracked item. "Specter" and "Dragon" are; "Ember" and "Frost"
    # are not, because they match themselves at 100. The exact-match escape is
    # what keeps real one-word names safe.
    reading_words = best[2].split()
    if (len(reading_words) == 1 and best[1] < 100
            and reading_words[0].lower() in decoy_words):
        return Decision(None, best[1], best_lead, best[2], rank=seen_rank, refinement=seen_refinement,
                        dismissed_as=reading_words[0])

    # A missing "Prime" is not evidence that the item is not Prime. OCR drops
    # whole words routinely - "Inaros Prime Chassis" has come back as "Ina
    # hassis" - and every Prime item has a non-Prime twin whose name is exactly
    # the same minus that word. So a reading that lost it lands on the twin
    # with a comfortable lead and is believed: "Gunse Blueprint" scored 96.8
    # against "Gunsen Blueprint" and 81.1 against "Gunsen Prime Blueprint", a
    # lead of 15.7 where 12 is enough to be certain. The user has the Prime.
    #
    # An exact match is the way through, as it is elsewhere here. "Oberon" is
    # Oberon at 100 and stays confident; anything less against a name with a
    # Prime twin is a question, and the review shows both.
    doubt = None
    doubt_word = None
    if best[1] < 100 and _has_prime_twin(best[0], names):
        doubt = (f"{best[0]} has a Prime version, and a reading that lost the "
                 f"word would look exactly like this")
    elif _head_score(best[2], best[0]) < HEAD_MATCH_MIN:
        # The first word of a name is the part that identifies it: hundreds of
        # names end "Prime Blueprint", and only one begins "Pandero". A reading
        # that lost the beginning scores high on what everything shares and
        # says nothing about which one it is - "le Prime Blueprint" matched Lex
        # Prime Blueprint at 97.3 and went straight into the inventory.
        doubt_word = best[0].split()[0]
        doubt = f"the reading does not clearly carry the word {doubt_word!r}"

    return Decision(best[0], best[1], best_lead, best[2], rank=seen_rank,
                    refinement=seen_refinement, doubt=doubt,
                    doubt_word=doubt_word)


def _tame_opencv_threads() -> None:
    """Stop OpenCV using its own thread pool.

    The scan forks a process pool from a worker thread, and OpenCV keeps a
    pool of its own. A fork copies only the forking thread, so any OpenCV lock
    another thread happened to hold stays locked forever in the child - and on
    some builds the parent's pool comes back from the fork in a state where its
    workers wait on a futex that will never be posted. The symptom is a scan
    that stops mid-pass with every thread in __futex_wait and no work being
    done, which is exactly what a hang looks like because it is one.

    Nothing is lost by switching it off: the parallelism that matters here is
    the process pool, and the per-image operations are small. Measured on a
    full scan, the difference in wall clock is inside the noise.

    Called just before the first pool is forked rather than at the top of the
    scan. The fork is what the single-threading protects, and pass one - which
    walks every frame of the recording through cvtColor and resize - runs
    before any fork exists. Letting OpenCV keep its threads for that pass
    saves about two milliseconds a frame, twenty seconds on a 4K recording,
    and gives the fork exactly what it had before: a parent whose OpenCV pool
    is switched off and idle.
    """
    try:
        import cv2

        cv2.setNumThreads(0)
    except Exception:  # noqa: BLE001 - an OpenCV without threading support is fine
        pass


def _sweep_old_workdirs(keep_hours: float = 6.0) -> None:
    """Delete frame directories left behind by earlier runs.

    A scan writes about 700MB of frames, and on a system where /tmp is a tmpfs
    that is 700MB of RAM. Two return paths used to skip the cleanup - a failed
    grid fit, and cancelling - so an interrupted scan left the lot behind.
    Eight of them had accumulated on one machine: 5.6GB of tmpfs, which
    is enough to push a machine into swap and make everything afterwards look
    like it has hung.

    The paths are fixed. This handles what is already lying around, and the
    runs that die in ways no `finally` can catch.
    """
    import glob

    cutoff = time.time() - keep_hours * 3600
    for path in glob.glob(os.path.join(tempfile.gettempdir(), "wft-scan-*")):
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def scan(video_path: str, connection: sqlite3.Connection,
         movement_step: float = frames_mod.MOVEMENT_STEP,
         progress=None, cancel=None) -> ScanResult:
    """Read a recording. `progress` receives a Progress; `cancel` is polled.

    Cancelling returns what was read so far rather than nothing: eighteen
    minutes of work should not evaporate because the user changed their mind at
    minute seventeen, and a partial result is still reviewable.
    """
    _sweep_old_workdirs()

    notes: list[str] = []
    started = time.perf_counter()

    # A folder of screenshots is the same job with pass one already done: the
    # video pass exists to turn a recording into pictures on disk, and a folder
    # is pictures on disk. Recording one category at a time is also the better
    # way to do this - the cosmetics tab is where most of the review queue
    # comes from - and that is easier with screenshots than with video.
    media_paths: list[str] = []
    if os.path.isdir(video_path):
        media_paths = frames_mod.media_files(video_path)
        if not media_paths:
            raise FileNotFoundError(
                f"{video_path} holds nothing to read ("
                f"{', '.join(frames_mod.IMAGE_SUFFIXES + frames_mod.VIDEO_SUFFIXES)})")
        videos = [p for p in media_paths if frames_mod.is_video(p)]
        pictures = len(media_paths) - len(videos)
        # A recording's frames are not known until it has been walked, so the
        # total is the sum of what each file offers. Cheap: a container's frame
        # count is in its header.
        frames_total = pictures + sum(
            frames_mod.total_frames(path) for path in videos)
        notes.append(
            f"folder: {len(videos)} recording(s) and {pictures} screenshot(s)")
    else:
        frames_total = frames_mod.total_frames(video_path)

    # Exponentially smoothed throughput. The weight is low so a brief stall does
    # not send the estimate wandering, and high enough that the change from
    # "static opening, no OCR" to "scrolling, OCR on every sample" is picked up
    # within a few seconds instead of dragging through the whole run.
    smoothed_rate: float | None = None
    last_time = started
    last_index = 0

    words: list[pm.Word] = []
    scrolls: dict[int, float] = {}
    #: Frames that were a still picture rather than a video frame. One sighting
    #: is all a screenshot can ever offer, so the sightings rule does not apply
    #: to them.
    still_frames: set[int] = set()
    #: frame index -> which capture in the folder it came from.
    frame_source: dict[int, int] = {}
    #: (row pitch, column pitch, strength) measured off sampled frames.
    pitches: list[tuple[int, int, float]] = []
    #: How much fine detail each sampled frame holds.
    sharpness: dict[int, float] = {}
    #: (height, width) of the frames, for turning the grid into fractions.
    frame_size: tuple = ()
    frames_read = 0

    with ocr_mod.Engine(psm=ocr_mod.PSM_BLOCK):
        pass  # fail early if no OCR backend is available

    # --- pass one: walk the video and write the sampled frames -------------
    #
    # Kept apart from OCR rather than interleaved. Both stages are CPU-bound on
    # the same cores, and running them together starves the pool: measured
    # during a real run, one tesseract process was alive out of fourteen and the
    # producer's tick was 240ms against 8ms measured idle. See
    # wft/inventory/ocrpool for the numbers.
    refs: list[ocrpool.FrameRef] = []
    workdir = tempfile.mkdtemp(prefix="wft-scan-")
    try:
        def announce(position: int, path: str, count: int) -> None:
            notes.append(f"  {position + 1}/{count} {os.path.basename(path)}")

        source = (frames_mod.sample_folder(media_paths,
                                           movement_step=movement_step,
                                           cancel=cancel, on_file=announce)
                  if media_paths
                  else frames_mod.sample(video_path,
                                         movement_step=movement_step,
                                         cancel=cancel))
        for sample in source:
            frames_read += 1
            # The grid, measured off the tile edges the game draws. Every tenth
            # kept frame is plenty and costs a few milliseconds; the answer is
            # the same at every scroll position, which is the point of taking
            # it from the picture rather than from the text.
            # Often enough to have a vote, then rarely. Every tenth frame was
            # fine for a long recording and left a short one with a single
            # measurement: the companions capture keeps seven frames, measured
            # its grid once, got 436 pixels where the tiles are 444, and used
            # that two percent for every crop it cut.
            if len(pitches) < PITCH_VOTES or frames_read % 10 == 1:
                found = pm.measure_pitch(sample.image)
                if found:
                    pitches.append(found)
            # How sharp this frame is, for choosing which one to read a tile
            # from. A frame caught mid-scroll is smeared, and the tile sitting
            # most centrally in it is still smeared.
            sharpness[sample.index] = pm.sharpness(sample.image)
            if not frame_size:
                frame_size = sample.image.shape[:2]
            scrolls[sample.index] = sample.scroll
            if sample.still:
                still_frames.add(sample.index)
            frame_source[sample.index] = sample.source
            refs.append(ocrpool.FrameRef(
                sample.index, sample.scroll,
                ocrpool.write_frame(sample.image, workdir, sample.index)))
            if progress:
                now = time.perf_counter()
                span, advanced = now - last_time, sample.index - last_index
                if span > 0.5 and advanced > 0:
                    instant = advanced / span
                    smoothed_rate = (instant if smoothed_rate is None
                                     else 0.7 * smoothed_rate + 0.3 * instant)
                    last_time, last_index = now, sample.index
                progress(Progress(sample.index, frames_total, 0, now - started,
                                  rate=smoothed_rate, stage="sampling"))
            if cancel is not None and cancel():
                notes.append(f"cancelled after {frames_read} frames — "
                             f"the partial result is still usable")
                break

        # --- pass two: read them across every core -------------------------
        _tame_opencv_threads()   # before the first fork, not before pass one
        total_refs = len(refs)
        read_started = time.perf_counter()

        def report(done: int) -> None:
            if not progress:
                return
            elapsed = time.perf_counter() - read_started
            progress(Progress(done, total_refs, len(words), elapsed,
                              rate=done / elapsed if elapsed > 0 else None,
                              stage="reading"))

        for index, scroll, found in ocrpool.read_files(refs, progress=report,
                                                       cancel=cancel):
            for word in found:
                words.append(pm.Word(
                    text=word["text"], x=float(word["left"]),
                    virtual_y=float(word["top"]) + scroll,
                    width=float(word["width"]), height=float(word["height"]),
                    confidence=float(cal_mod.MIN_WORD_CONFIDENCE),
                    frame_index=index))
        # Frames stay on disk until the evidence crops have been cut from
        # them; the grid is only known after all the words are in.
        frame_files = {ref.index: ref.path for ref in refs}
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    was_cancelled = cancel is not None and cancel()
    words_total = len(words)
    # Put the words in a fixed order before anything reads them.
    #
    # They arrive from `imap_unordered`, so which process finishes first
    # decides the order they land in - and that order survives into the cells,
    # where it decides which of two equally-scoring hypotheses a tile is read
    # as. Two runs of identical code over the same folder gave 251 and 252
    # tiles, differing by exactly one relic: "Lith F1" in one and "Lith O4" in
    # the other. A scan whose answer depends on process scheduling cannot be
    # measured, and this project is measured for a living.
    words.sort(key=lambda w: (w.frame_index, w.virtual_y, w.x, w.text))
    words = pm.drop_screen_fixed(words, scrolls)

    measured = None
    remembered = dbq.tile_grid(connection)
    if remembered and frame_size:
        # What this screen said last time. The grid depends on the resolution
        # and the interface scale, not on the capture, so a recording too
        # short to measure it well borrows the answer instead of guessing one.
        borrowed = (int(round(remembered[0] * frame_size[0])),
                    int(round(remembered[1] * frame_size[1])), 0.0)
        if len(pitches) < PITCH_VOTES:
            notes.append(f"tile grid remembered from an earlier scan: "
                         f"{borrowed[0]}px rows, {borrowed[1]}px columns")
            pitches = [borrowed]
    if pitches:
        # The median of what was measured, not the mean: one frame with a
        # dialog over the shelf should not move the answer.
        rows = sorted(p[0] for p in pitches)
        columns = sorted(p[1] for p in pitches)
        measured = (rows[len(rows) // 2], columns[len(columns) // 2],
                    max(p[2] for p in pitches))
        spread = rows[-1] - rows[0]
        notes.append(f"tile grid measured off the picture: {measured[0]}px rows, "
                     f"{measured[1]}px columns, from {len(pitches)} frame(s)"
                     + (f" (spread {spread}px)" if spread else ""))
        # Remembered for the next capture, as a fraction of the frame.
        if len(pitches) >= PITCH_VOTES and frame_size:
            try:
                dbq.set_tile_grid(connection, measured[0] / frame_size[0],
                                  measured[1] / frame_size[1])
            except Exception:  # noqa: BLE001
                pass
    grid = pm.build(words, notes, measured=measured)
    if grid is None:
        shutil.rmtree(workdir, ignore_errors=True)
        return ScanResult([], frames_read, words_total, len(words), 0, None,
                          notes, was_cancelled)

    # Take the accumulated scroll error out before anything is filed by row.
    settled = pm.settle(words, grid, scrolls, sources=frame_source)
    # Guarded here as well as inside _dump_state: the fingerprint walks every
    # word of the recording, and Python evaluates arguments before the callee
    # can decline them - 78ms of hashing per scan for a file nobody asked for.
    if os.environ.get("WFT_DEBUG_STATE"):
        _dump_state('geometry', grid=grid, scrolls=sorted(scrolls.items()),
                    words=(len(words), _fingerprint(
                        f"{w.frame_index}:{w.virtual_y:.2f}:{w.x:.2f}:{w.text}"
                        for w in words)))
    if settled:
        notes.append(f"{settled} frame(s) nudged onto the row grid — the "
                     f"scroll offset drifts as it accumulates, and half a row "
                     f"of it files one tile under two rows")
    cells = pm.assign(words, grid)
    lookup = _tracked_names(connection)
    names = list(lookup)
    ignored = _ignored_names(connection)
    learned = dbq.learned_ignores(connection)
    # Every word that appears in a decoy name. A tile whose name wraps to two
    # lines can have one line assigned as a cell of its own, and a single word
    # scores badly against the full decoy it came from: "Specter" reaches only
    # 78 against "Ancient Protector Specter" and so lost to Spectra at 86. As a
    # word it is an exact hit, which is a far cheaper test than a substring
    # search over seventeen thousand names.
    decoy_words = {word for name in ignored for word in name.lower().split()
                   if len(word) >= 4}

    candidates: list[Candidate] = []
    for (column, row), cell_words in cells.items():
        seen_in = {w.frame_index for w in cell_words}
        sightings = len(seen_in)
        if sightings < MIN_SIGHTINGS and not seen_in & still_frames:
            continue

        decision = decide_cell(pm.cell_hypotheses(cell_words, grid.row_pitch),
                               names, ignored, decoy_words, learned)
        if decision.name is None:
            continue

        ident = lookup[decision.name]
        sources = {frame_source.get(w.frame_index, 0) for w in cell_words}
        candidates.append(Candidate(
            column=column, row=row, name=decision.name, score=decision.score,
            reading=decision.reading, sightings=sightings, lead=decision.lead,
            part_unique=ident.part, item_unique=ident.item,
            relic_unique=ident.relic, rank=decision.rank,
            refinement=decision.refinement, doubt=decision.doubt,
            doubt_word=decision.doubt_word,
            sources=frozenset(sources),
            from_still=bool(seen_in & still_frames)))

    # Cancelling skips everything after the reading pass. It used to stop only
    # the pass it was raised in and then run the finishing work anyway - the
    # re-read, the badges, the crops - so a cancel meant waiting minutes for
    # results that were being thrown away. The candidates found so far are
    # still returned, without pictures or stack counts.
    # Re-checked here, not reused: the flag above was taken before the reading
    # pass, and cancelling during that pass is the common case.
    was_cancelled = cancel is not None and cancel()
    if was_cancelled:
        notes.append("cancelled — name re-reading and stack counts skipped")
        shutil.rmtree(workdir, ignore_errors=True)
        return ScanResult(candidates, frames_read, words_total, len(words),
                          len(cells), grid, notes, True)

    # The passes after reading are not instant and used to report nothing, so
    # the bar sat at 99% with "0s left" while the re-read worked through the
    # review queue - a minute or more of looking exactly like a hang. They get
    # their own stage.
    review_count = sum(1 for c in candidates if not c.confident)

    def finishing(done: int, what: str, total: int | None = None) -> None:
        if progress and total:
            progress(Progress(done, total, len(words),
                              time.perf_counter() - started, stage=what))

    # One decoded copy of each frame, shared by every pass from here down.
    # They walk the same few hundred JPEGs and used to decode their own.
    frame_cache = _FrameCache()

    # Before anything that works from the candidate list. A tile the page pass
    # never recognised is not in that list, and every pass after this one is
    # driven by it - so a tile found later than here would get no picture, no
    # stack count and no re-read.
    _sweep_frames(candidates, cells, grid, frame_files, scrolls, still_frames,
                  frame_source, lookup, ignored, decoy_words, learned, notes,
                  sharpness=sharpness, cache=frame_cache,
                  progress=lambda done: finishing(done, "sweeping the grid",
                                                  len(frame_files)))

    drift: list[float] = []
    _dump_state('before-reread', found=sorted(
        (c.column, c.row, c.name, round(c.score, 2), c.quantity, c.reading)
        for c in candidates))
    rescued, discarded = _attach_evidence(
        candidates, cells, grid, frame_files, scrolls, lookup=lookup, drift=drift,
        ignored=ignored, decoy_words=decoy_words, learned=learned,
        cache=frame_cache,
        progress=lambda done: finishing(done, "re-reading names", review_count))

    # After the re-read, not before: the re-read changes what a tile matched and
    # what it scored, so checking first meant a demoted repeat could be re-read
    # back into confidence and auto-accepted, and a tile re-read onto an
    # already-claimed item was never checked at all.
    #
    # The cost is a second pass over the frames for pictures, because a repeat
    # is only known to need one after this point. It is a few dozen tiles.
    # Before the repeat check, which would otherwise ask about every one of
    # them as if a second Nikana had turned up.
    _merge_overlapping_stills(candidates, notes)

    _dump_state('tiles', found=sorted(
        (c.column, c.row, c.name, round(c.score, 2), c.quantity)
        for c in candidates))
    _one_tile_per_relic(candidates, notes)
    repeats = _flag_repeated_items(candidates, notes)
    if repeats:
        # Re-read them too, not just picture them. A repeat is usually a skin,
        # and the band often says so outright - "Ash Prime Helmet" where the
        # page pass saw only "Ash". Reading it is what turns the question into
        # an answer, and it is the same few dozen tiles either way.
        rescued_again, discarded_again = _attach_evidence(
            candidates, cells, grid, frame_files, scrolls, lookup=lookup,
            ignored=ignored, decoy_words=decoy_words, learned=learned,
            cache=frame_cache,
            progress=lambda done: finishing(done, "checking repeats", repeats))
        rescued += rescued_again
        discarded += discarded_again

    if rescued:
        notes.append(f"{rescued} reading(s) recovered by re-reading the name band")
    if discarded:
        notes.append(f"{discarded} tile(s) turned out to be untracked on a "
                     f"second read")

    _resolve_relic_refinements(candidates, connection, notes)

    if drift:
        import statistics

        typical = statistics.median(abs(value) for value in drift)
        worst = max(abs(value) for value in drift)
        notes.append(
            f"grid fit: tiles sat a median {typical:.1%} of a row off, worst "
            f"{worst:.1%} — each was snapped to its own name")
        # A fit that is out by a third of a row everywhere is not a fit that
        # snapping rescues, it is a fit that is wrong. Said plainly rather than
        # left in a percentage nobody reads.
        if typical > 0.15:
            notes.append(
                "the fitted grid is a long way from where the tiles actually "
                "are — check the recording for a changed window size or a "
                "scroll that jumped")

    stacked = _read_quantities(
        candidates, cells, grid, frame_files, scrolls, notes=notes,
        cache=frame_cache,
        progress=lambda done, total=None: finishing(
            done, "reading stack counts", total or len(candidates)))
    if stacked:
        notes.append(f"{stacked} tile(s) carried a stack badge")
    # Keep the frames candidates point at, small enough to hold. Before the
    # workdir goes: after it, the only way back to a frame is to seek the
    # original recording, which costs seconds per click and needs a timestamp
    # nothing currently carries.
    kept_frames = _keep_context_frames(candidates, frame_files,
                                       cache=frame_cache)
    _dump_state('frame-cache', hits=frame_cache.hits,
                misses=frame_cache.misses)

    # The written frames are a temporary artefact, not a cache: large, and
    # specific to one run's sampling step.
    shutil.rmtree(workdir, ignore_errors=True)

    return ScanResult(candidates, frames_read, words_total, len(words),
                      len(cells), grid, notes, was_cancelled, kept_frames)
