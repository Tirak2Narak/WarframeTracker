"""One grid for the whole recording, not one per frame.

Estimating the grid from a single frame was the mistake. The same video gave
7 columns or 9, a pitch of 415 px or 431 px, depending which frame was caught;
on one recording the read rate fell from 15/18 to 1/8 after a change that only
altered *which* frames were sampled. Frame-local estimation is fragile because a
single frame can be caught mid-scroll, behind a dialog, or on a sparse page.

But the geometry does not change during a recording. It has four unknowns:

    column pitch, column phase, row pitch, row phase

and the row phase moves per frame by exactly the scroll offset the tracker
already measures. So every OCR word from every frame can be placed on one
**virtual page** by adding the accumulated scroll to its y position, and the
four parameters estimated once from all of them together. Hundreds of frames
average out the dialogs, the blurred frames and the sparse pages.

Two things follow that are worth more than the robustness:

* **Cells are assigned by interval, not by gap.** A word belongs to the column
  whose centre it is nearest, within half a pitch. Merging two names is then
  impossible by construction — where gap-based splitting depends on a threshold
  that fails on long names, which is what forced the previous attempt into a
  194-entry blocklist.
* **A wrapped name needs no special handling.** "Lavos Prime Neuroptics" and the
  "Blueprint" centred beneath it land in the same cell because both fall in the
  same interval. That single fact is what the blocklist was standing in for.

The column count is not estimated at all: Warframe's inventory has six columns.
Deriving it was wrong twice over — image width over pitch counted the 4K detail
pane as three extra columns, and the span of successfully read tiles depended on
whether an edge tile happened to OCR cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

#: Columns in the inventory grid. A property of the game.
COLUMNS = 6

#: Bin width for the position histograms, in pixels. Fine enough to separate
#: columns that sit ~415 px apart, coarse enough that a few stray words do not
#: create their own peak.
BIN_WIDTH = 8

#: A row is only accepted if this many words landed in it across the whole
#: recording. Filters out the header, the footer and one-off noise.
MIN_WORDS_PER_ROW = 3


@dataclass
class Word:
    """One OCR word, positioned on the virtual page."""

    text: str
    x: float            # left edge, frame coordinates (constant across frames)
    virtual_y: float    # y plus accumulated scroll: position on the whole page
    width: float
    height: float
    confidence: float
    frame_index: int


@dataclass
class Grid:
    """The four parameters, estimated once for a whole recording."""

    column_pitch: float
    column_phase: float     # centre of column 0
    row_pitch: float
    row_phase: float        # centre of row 0 on the virtual page
    #: Row pitch as it appears on screen, measured off the tile edges rather
    #: than fitted to word positions. Zero when no frame could be measured.
    #: Not the same number as `row_pitch`: that one lives on the virtual page,
    #: which is glued together along an estimated scroll offset and can be
    #: compressed relative to the screen it was read from.
    screen_row_pitch: float = 0.0
    columns: int = COLUMNS
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    def column_of(self, x_centre: float) -> int | None:
        """Which column a word centre belongs to, or None if outside the grid."""
        index = round((x_centre - self.column_phase) / self.column_pitch)
        if index < 0 or index >= self.columns:
            return None
        # Reject words further than half a pitch from the column centre: those
        # sit in the detail pane or in UI chrome, not in a tile.
        centre = self.column_phase + index * self.column_pitch
        if abs(x_centre - centre) > self.column_pitch * 0.5:
            return None
        return int(index)

    def row_of(self, virtual_y: float) -> int:
        return int(round((virtual_y - self.row_phase) / self.row_pitch))

    def phase_in_row(self, virtual_y: float) -> float:
        """Where inside its row a word sits, 0.0 to 1.0. For diagnostics.

        A name band was tried here and removed. Guessing that names live at
        0.55 to 1.00 of the row let header text in and dropped the item names,
        halving the hit rate. The artwork noise it was meant to exclude is
        better handled by matching against the candidate list, where
        'N 4 7 W 74 N' has nothing to match.

        Kept as a measurement: if the matched names ever stop clustering at one
        phase, the grid is wrong, and that is worth seeing.
        """
        return ((virtual_y - self.row_phase) % self.row_pitch) / self.row_pitch


def _histogram_peaks(values: np.ndarray, bin_width: float,
                     expected: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Histogram of positions, returned as (bin centres, counts)."""
    if values.size == 0:
        return np.array([]), np.array([])
    low, high = float(values.min()), float(values.max())
    bins = max(4, int((high - low) / bin_width) + 1)
    counts, edges = np.histogram(values, bins=bins, range=(low, high + bin_width))
    centres = (edges[:-1] + edges[1:]) / 2
    return centres, counts


def _best_periodic_fit(values: np.ndarray, count: int,
                       pitch_range: tuple[float, float]) -> tuple[float, float, float]:
    """Fit `count` equally spaced positions to the data.

    Returns (pitch, phase, score). Knowing how many positions to expect turns
    this from "find whatever repeats" into a two-parameter fit, which is far
    better conditioned — the reason the column count is worth knowing rather
    than measuring.

    Score is the share of values that land within a quarter pitch of a predicted
    position, so an unrelated distribution scores near zero instead of returning
    a confident wrong answer.
    """
    if values.size < count:
        return 0.0, 0.0, 0.0

    low, high = pitch_range
    best = (0.0, 0.0, 0.0)
    # Coarse-to-fine: a full sweep at 1 px would cost little here, but the two
    # passes keep this usable if the range ever widens.
    for step, span in ((4.0, (low, high)), (0.5, None)):
        if span is None:
            centre = best[0]
            if centre <= 0:
                break
            span = (centre - 6, centre + 6)
        pitch = span[0]
        while pitch <= span[1]:
            # For a given pitch, the best phase is where the values cluster
            # modulo that pitch.
            residues = values % pitch
            # Circular mean, so a cluster straddling 0 and pitch is not split.
            angles = residues / pitch * 2 * np.pi
            phase = float(np.arctan2(np.sin(angles).mean(),
                                     np.cos(angles).mean()) / (2 * np.pi) * pitch)
            phase %= pitch
            distance = np.abs(((values - phase + pitch / 2) % pitch) - pitch / 2)
            score = float((distance < pitch / 4).mean())
            if score > best[2]:
                best = (pitch, phase, score)
            pitch += step
    return best


#: Where to look for a tile pitch, in pixels of a 2160-pixel-high picture.
#: Wide enough for a small window and a 4K one; narrow enough to exclude the
#: spacing between the two lines of a wrapped name, which is the trap every
#: earlier attempt fell into.
PITCH_SEARCH = (250, 700)

#: The height those pixels are for. The interface scales with the window, so a
#: 1080-pixel-high picture has tiles half as tall (222px where 4K has 444), and
#: a search written in pixels is a search for a 4K window.
#:
#: Measured on the inventory fixtures through the folder import: at
#: 1920x1080 the old range started above the real pitch and the height check
#: below (1080 < 2 x 700) returned None before looking at all, so every 1080p
#: capture fell back to the fit from the words - the one this function exists
#: to replace, which was 8.1% of a row off at 1080p where the measured grid is
#: 1.8% off at 4K.
REFERENCE_HEIGHT = 2160


def pitch_search(height: int) -> tuple[int, int]:
    """`PITCH_SEARCH` for a picture this high: the same range in tile heights."""
    scale = height / REFERENCE_HEIGHT
    return max(8, round(PITCH_SEARCH[0] * scale)), round(PITCH_SEARCH[1] * scale)

#: How far the fitted pitch may sit from the measured one before the measured
#: one is used instead. The measurement comes from the tile edges the game
#: draws and does not care what the names look like; the fit comes from word
#: positions and does. They agree to a pixel or two on a good capture.
PITCH_TOLERANCE = 0.04

#: How far the column fit may stray from the measured column pitch before the
#: measurement is asked. Columns sit on the screen, not on the scrolled page,
#: so unlike the row pitch the measured value is the same quantity the
#: fit estimates. A sparse tab - 1 to 8 named tiles - gives the fit too few
#: word positions to be well conditioned: three such screenshots scanned alone
#: fitted a column pitch of 546px (or 158px as PNG) where the edges measure
#: 416, and with it 482px rows, so 5 of the 12 tiles were lost; scanned inside
#: a folder with denser tabs the same pictures fitted correctly.
COLUMN_BAND = 0.10

#: How much worse the fit near the measurement may score and still be taken
#: over a fit outside it. The measurement is not always right: it can be
#: borrowed from an earlier scan of another frame shape (a crop), and a dense
#: page then fits its true pitch clearly better than the borrowed one. On the
#: sparse screenshots above the fit near 416 scored 0.019 (JPG) and 0.011 (PNG)
#: below the wrong free fit - a tie in all but name.
COLUMN_SCORE_SLACK = 0.05


def measure_pitch(image) -> tuple[int, int, float] | None:
    """Row and column pitch, measured off the tile edges rather than the text.

    The grid is drawn on screen: between tiles are gaps, and a line of pixels
    running through a gap is uniform where one running through artwork is not.
    So the standard deviation along each row of pixels is a periodic signal
    whose period is the row pitch, and its autocorrelation finds that period
    without any knowledge of where the text sits.

    This exists because the fit from words is not measuring the grid, it is
    measuring the text. Fitted per capture on real recordings it returned
    477, 404, 487, 400 and 397 pixels for five files of the same interface -
    the categories where names wrap to two lines pulled it one way and the ones
    where they do not pulled it the other. Measured this way, all five come
    back 444, at every sampled position, which is what "the tiles are all the
    same size" looks like when it is true.

    Returns (row_pitch, column_pitch, confidence) or None when neither axis
    shows a period - a frame holding one row of tiles has nothing to lock onto,
    and saying so is better than returning a number from noise.
    """
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    height, width = grey.shape
    search = pitch_search(height)
    if height < search[1] * 2 or width < search[1] * 2:
        return None

    # The tile area only. The detail pane on the right is static and would
    # answer with its own layout; the header would answer with none.
    tiles = grey[:, int(width * 0.03):int(width * 0.66)]
    row = _dominant_period(tiles.std(axis=1), search)
    column = _dominant_period(
        grey[int(height * 0.2):int(height * 0.9), :int(width * 0.68)].std(axis=0),
        search)
    if row is None or column is None:
        return None
    return row[0], column[0], min(row[1], column[1])


def _dominant_period(profile, search=PITCH_SEARCH) -> tuple[int, float] | None:
    """The strongest repeat in a 1-D signal, by autocorrelation."""
    lo, hi = search
    if profile.size < hi * 2:
        return None
    centred = profile - profile.mean()
    correlation = np.correlate(centred, centred, mode="full")[centred.size - 1:]
    if correlation[0] <= 0:
        return None
    correlation = correlation / correlation[0]
    peak = lo + int(np.argmax(correlation[lo:hi]))
    strength = float(correlation[peak])
    # A flat profile correlates with itself at every lag. Below this the peak
    # is noise, and a pitch from noise is worse than no pitch.
    if strength < 0.2:
        return None
    return peak, strength


def sharpness(image) -> float:
    """How much fine detail a frame holds, as one number.

    A frame caught mid-scroll is smeared, and a smeared name band reads as
    `'Jrirmvans FAarHAarms Drimaa'` where a still one reads "Wisp Prime
    Neuroptics". Choosing the frame where a tile sits furthest from the screen
    edge says nothing about that - the most central sighting can be the
    blurriest one in the recording.

    The variance of a Laplacian is the standard measure and costs about a
    millisecond on a downscaled frame. Only the tile area is looked at, and
    only every fourth pixel: this is a comparison between frames of the same
    scene, so it has to be consistent, not absolute.
    """
    height, width = image.shape[:2]
    tiles = image[int(height * 0.15):int(height * 0.95),
                  int(width * 0.03):int(width * 0.66):4]
    grey = cv2.cvtColor(tiles[::4], cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


def gap_phase(image, pitch: float) -> float | None:
    """Where a tile boundary sits in this frame, in screen pixels.

    A row of pixels running through the gap between two tile rows is uniform;
    one running through artwork is not. So the standard deviation along each
    row, scored against a comb of the known pitch, finds the phase that lands
    on gaps - the boundary itself, measured rather than inferred from where the
    text happens to sit.

    Only the tile area is looked at. The detail pane on the right is static and
    has its own horizontal structure, which dilutes the comb exactly as it
    diluted the scroll measurement before `frames.MEASURE_WIDTH` was introduced
    for the same reason.
    """
    height, width = image.shape[:2]
    if height < pitch * 2:
        return None
    grey = cv2.cvtColor(image[:, int(width * 0.03):int(width * 0.66)],
                        cv2.COLOR_BGR2GRAY).astype(np.float32)
    spread = grey.std(axis=1)
    spread = (spread - spread.mean()) / (spread.std() + 1e-9)

    best_phase, best_score = None, None
    for phase in np.arange(0.0, pitch, 2.0):
        rows = np.arange(phase, height, pitch).astype(int)
        rows = rows[rows < height]
        if len(rows) < 3:
            continue
        # Gaps are the quiet rows, so the comb is scored on the negative.
        score = -float(spread[rows].mean())
        if best_score is None or score > best_score:
            best_phase, best_score = float(phase), score

    # A frame showing a dialog, or one caught mid-transition, has no clean comb.
    # Saying "no answer" lets the caller fall back rather than crop at random.
    if best_score is None or best_score < 0.35:
        return None
    return best_phase


def build(words: list[Word], notes: list[str] | None = None,
          measured: tuple[int, int, float] | None = None) -> Grid | None:
    """Estimate the grid from every word of a whole recording.

    `measured` is the pitch read off the tile edges by `measure_pitch`. It is
    carried through rather than fed into the fit, because the two are not the
    same quantity: the fit works on the virtual page, which is every frame
    glued together along an estimated scroll offset, and a slightly compressed
    estimate gives that page a different row pitch than the screen has. Forcing
    the screen's 444 pixels onto a page whose own pitch fitted to 383 made
    every result worse - measured, not guessed.

    So the fitted pitch keeps deciding which page row a tile belongs to, and
    the measured one decides how tall a crop is in a frame.
    """
    notes = notes if notes is not None else []
    if len(words) < 40:
        return None

    centres_x = np.array([w.x + w.width / 2 for w in words])

    # Columns: six equally spaced positions. The pitch range spans a plausible
    # tile width for any capture from a small window to 4K.
    pitch, phase, score = _best_periodic_fit(centres_x, COLUMNS, (150.0, 700.0))
    if measured and measured[1] > 0:
        band = (max(150.0, measured[1] * (1 - COLUMN_BAND)),
                min(700.0, measured[1] * (1 + COLUMN_BAND)))
        # Only a fit outside the band is questioned. Searching the band anew
        # when the fit was already in it moved 208.5px to 219.2 on the 1080p
        # fixture (the coarse steps start elsewhere) and lost a tile.
        if not band[0] <= pitch <= band[1]:
            near = _best_periodic_fit(centres_x, COLUMNS, band)
            if near[0] > 0 and near[2] >= score - COLUMN_SCORE_SLACK:
                pitch, phase, score = near
    if pitch <= 0 or score < 0.35:
        notes.append(f"column fit too weak (score {score:.2f}) — "
                     f"is this an inventory recording?")
        return None

    # The fit gives the phase modulo the pitch; which multiple is column zero is
    # a separate question. Choose the offset that captures the most words —
    # walking back to the leftmost word instead put column zero at -94 px, off
    # the frame, and shifted every later column by one.
    column_phase = phase
    best_covered = -1
    candidate = phase % pitch
    top = centres_x.max()
    while candidate < top:
        # One numpy pass instead of two round() calls per word: same banker's
        # rounding, same answer, and this loop was most of build()'s cost.
        nearest = np.round((centres_x - candidate) / pitch)
        covered = int(np.count_nonzero(
            (nearest >= 0) & (nearest < COLUMNS)
            & (np.abs(centres_x - (candidate + nearest * pitch)) < pitch * 0.5)))
        if covered > best_covered:
            best_covered, column_phase = covered, candidate
        candidate += pitch

    # Rows: the same fit, but on the virtual page and with an unknown count, so
    # the pitch comes from the spacing of populated rows instead.
    virtual = np.array([w.virtual_y + w.height / 2 for w in words])
    row_centres, row_counts = _histogram_peaks(virtual, BIN_WIDTH)
    populated = row_centres[row_counts >= MIN_WORDS_PER_ROW]
    if populated.size < 4:
        notes.append("too few populated rows to estimate the row pitch")
        return None

    # The row pitch is NOT the smallest recurring gap. A wrapped name puts a
    # second text line about 55 px below the first, and taking the median gap
    # locks onto that — measured, it returned 55 px where the rows are 444 apart,
    # which is the same trap that defeated the earlier per-frame calibration.
    #
    # Inventory tiles are close to square, so the row pitch is near the column
    # pitch. Searching that band excludes the intra-name spacing by construction
    # rather than by a threshold.
    row_pitch, row_phase, row_score = _best_periodic_fit(
        virtual, 0, (pitch * 0.8, pitch * 1.6))
    if row_pitch <= 0 or row_score < 0.25:
        notes.append(f"row fit weak (score {row_score:.2f})")
        if row_pitch <= 0:
            return None

    return Grid(column_pitch=pitch, column_phase=column_phase,
                row_pitch=row_pitch, row_phase=row_phase,
                screen_row_pitch=float(measured[0]) if measured else 0.0,
                confidence=min(score, row_score if row_score else score),
                notes=notes)


def settle(words: list[Word], grid: Grid, scrolls: dict, sources=None) -> int:
    """Put every frame on the row comb, carrying the correction forward.

    The scroll offset is measured between consecutive frames and accumulates:
    accurate per step, and by the end of a capture the page is stretched or
    squeezed by a percent or two. The grid is fitted once over every capture in
    a folder, so its comb is a compromise, and the error against it grows with
    the row number - two pixels of pitch error is 184 pixels, four tenths of a
    row, by row ninety.

    At that point two printed names land in one cell. `row_of` bins by the
    nearest row centre, one decision is made for the pair, and the other tile
    is gone. Read on its own the same capture sits at rows 0 to 15, the same
    error is thirty pixels, and nothing is lost - which is exactly what the
    measurements showed: 52 relics of 55 alone, 47 in a folder, the holes
    scattered across cells whose neighbours were fine.

    The correction is *carried*, not capped. An earlier version refused any
    step beyond three tenths of a row, meaning to reject a bad reading - but
    that is a dead zone across precisely the frames that need help. Between
    two consecutive kept frames the true residual moves by pixels, so wrapping
    each one relative to the last is unambiguous however far the total has
    drifted.

    Credit: Fable diagnosed both the dead zone and the unwrap.
    """
    import numpy as np
    from collections import defaultdict

    if grid is None or not words:
        return 0
    by_frame: dict[int, list[Word]] = defaultdict(list)
    for word in words:
        by_frame[word.frame_index].append(word)

    per_source: dict[int, list[int]] = defaultdict(list)
    for index in by_frame:
        per_source[(sources or {}).get(index, 0)].append(index)

    moved = 0
    pitch = grid.row_pitch
    half = pitch / 2
    for indexes in per_source.values():
        # In scroll order, so "the previous frame" means the one just before
        # it on the page and the unwrap has something short to reach back to.
        carried = 0.0
        for index in sorted(indexes, key=lambda i: scrolls.get(i, 0.0)):
            group = by_frame[index]
            if len(group) >= 3:
                centres = np.array([w.virtual_y + w.height / 2 for w in group])
                shifted = centres - carried - grid.row_phase
                residual = float(np.median(((shifted + half) % pitch) - half))
                carried += residual
            # A frame with almost no words keeps its neighbour's correction
            # rather than none: it is on the same page and drifted the same way.
            if abs(carried) < 1.0:
                continue
            for word in group:
                word.virtual_y -= carried
            if index in scrolls:
                scrolls[index] -= carried
            moved += 1
    return moved


def assign(words: list[Word], grid: Grid) -> dict[tuple[int, int], list[Word]]:
    """Place every word into its cell. Words outside the grid are dropped.

    Dropping is deliberate and is the replacement for a blocklist: header text,
    the detail pane and HUD chrome sit outside the column intervals, so they
    disappear without anyone having to enumerate them.
    """
    cells: dict[tuple[int, int], list[Word]] = {}
    for word in words:
        column = grid.column_of(word.x + word.width / 2)
        if column is None:
            continue
        centre_y = word.virtual_y + word.height / 2
        row = grid.row_of(centre_y)
        cells.setdefault((column, row), []).append(word)
    return cells


def drop_screen_fixed(words: list[Word], scroll_by_frame: dict[int, float],
                      tolerance: float = 12.0, min_frames: int = 3) -> list[Word]:
    """Remove UI text that stays put while the tiles scroll.

    Header and footer are fixed to the screen; tiles are not. So after adding
    the scroll offset, a tile word lands at one place on the virtual page while
    "INVENTORY/SELL" is smeared across as many places as there were scroll
    positions — and conversely returns to the same *screen* position every time.

    Detecting it that way needs no geometry and no threshold on where things
    ought to be. It is the structural version of what a name band was trying to
    approximate, and it also disposes of the blocklist question: nobody has to
    enumerate the UI words.
    """
    by_screen: dict[tuple[int, int, str], set[int]] = {}
    for word in words:
        screen_y = word.virtual_y - scroll_by_frame.get(word.frame_index, 0.0)
        key = (int(word.x / tolerance), int(screen_y / tolerance), word.text)
        by_screen.setdefault(key, set()).add(word.frame_index)

    fixed = {key for key, frames in by_screen.items() if len(frames) >= min_frames}
    if not fixed:
        return words

    kept = []
    for word in words:
        screen_y = word.virtual_y - scroll_by_frame.get(word.frame_index, 0.0)
        key = (int(word.x / tolerance), int(screen_y / tolerance), word.text)
        if key not in fixed:
            kept.append(word)
    return kept


def cell_hypotheses(words_in_cell: list[Word], row_pitch: float) -> list[str]:
    """Every plausible reading of one cell, for the matcher to choose between.

    Not one glued-together string. A cell holds a name, possibly wrapped, plus
    whatever OCR made of the artwork. Joining all of it produces a string that
    matches nothing well; offering the alternatives lets the candidate list
    decide, and there is exactly one real name per tile.

    Hypotheses are: each OCR line on its own, and each pair of vertically
    adjacent lines joined — the latter reassembles "Lavos Prime Neuroptics" and
    the "Blueprint" beneath it.

    Order is part of the answer, so they are collected in a dict rather than a
    set. `decide_cell` keeps the first of several equally-scoring hypotheses,
    and Python re-seeds string hashing in every process, so a set handed it a
    different order on every run: one cell read as "Lith O3 Relic" or
    "Lith Q3 Relic" - two hypotheses from two frames, each an exact match to a
    real relic, both scoring 100 - and another as "Limbo" or "LIMBO". Six runs
    over the same folder disagreed about two to four tiles this way, which made
    every measurement of a one-tile change meaningless.

    Insertion order is the frame order and then the reading order down the
    tile, which is the order to prefer anyway: the first frame that saw the
    cell is the one the rest of the geometry was fitted to.
    """
    by_frame: dict[int, list[Word]] = {}
    for word in words_in_cell:
        by_frame.setdefault(word.frame_index, []).append(word)

    hypotheses: dict[str, None] = {}
    for frame_words in by_frame.values():
        ordered = sorted(frame_words, key=lambda w: (w.virtual_y, w.x))
        lines: list[list[Word]] = []
        for word in ordered:
            if lines and abs(word.virtual_y - lines[-1][-1].virtual_y) <= word.height * 0.8:
                lines[-1].append(word)
            else:
                lines.append([word])

        texts = [" ".join(w.text for w in sorted(line, key=lambda w: w.x))
                 for line in lines]
        for text in texts:
            hypotheses[text] = None
        for first, second in zip(texts, texts[1:]):
            hypotheses[f"{first} {second}"] = None
        if len(texts) > 1:
            hypotheses[" ".join(texts)] = None
    return [h for h in hypotheses if h.strip()]


def cell_text(words_in_cell: list[Word]) -> str:
    """Reconstruct the name from the words of one cell, one frame at a time.

    Words are grouped by the frame they came from, ordered top-to-bottom then
    left-to-right, and joined. A wrapped name reassembles itself because both
    of its lines are in this cell — no rule needed, and nothing to block.
    """
    by_frame: dict[int, list[Word]] = {}
    for word in words_in_cell:
        by_frame.setdefault(word.frame_index, []).append(word)

    readings = []
    for frame_words in by_frame.values():
        ordered = sorted(frame_words, key=lambda w: (round(w.virtual_y / 20), w.x))
        readings.append(" ".join(w.text for w in ordered))
    return max(readings, key=len) if readings else ""
