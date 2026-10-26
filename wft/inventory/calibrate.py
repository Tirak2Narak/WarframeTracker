"""Measuring the inventory grid from the text itself.

The obvious approach — find the tile borders by projecting edge energy — gets
the *pitch* right and the *offset* wrong. Measured on a real recording it
reported a column pitch of 415.1 px against 414 px from an independent manual
measurement, but anchored half a tile off, and OCR on the resulting crops
returned nothing usable.

The reason is that tile edges are not what matters. What matters is where the
name text sits, and that can be measured directly: run OCR once over a still
frame, and the words arrange themselves into the grid.

    y=346   11 words  ┐ one row of names
    y=790   12 words  ┘ 444 px below — the row pitch
    tile centres in that row: 1460, 1875, 2291 — 415 px apart

Both numbers match the edge-projection result, so the pitch is confirmed from
two directions, and the anchor comes from the text rather than being guessed.
This also fails loudly: no text found means no calibration, instead of a
plausible grid over the wrong pixels.
"""
from __future__ import annotations

import csv
import subprocess
from dataclasses import dataclass, field

import cv2
import numpy as np

#: Words below this OCR confidence are ignored while calibrating. High on
#: purpose — calibration needs a few reliable anchors, not full coverage.
MIN_WORD_CONFIDENCE = 40.0

#: Vertical distance within which two words count as the same text line.
LINE_TOLERANCE = 25

#: Horizontal gap above which two words belong to different tiles. Well below
#: the tile pitch and well above a space between words.
TILE_GAP = 150

#: The inventory grid has six columns. A constant of the game, not a
#: measurement — both attempts to derive it were wrong in different ways:
#: image width over column pitch counted the 4K detail pane as three extra
#: columns (9), and the span of successfully read tiles depended on whether an
#: edge tile happened to OCR cleanly (7).
#:
#: Knowing the count makes the grid better determined rather than merely
#: simpler: the search is for six equidistant peaks, not for however many
#: happen to stand out.
INVENTORY_COLUMNS = 6

#: A frame must show at least this many tiles in one row, at regular spacing,
#: before it counts as showing the grid. A still frame is not automatically an
#: inventory frame: menus, dialogs and transitions are still too, and one of
#: them ("SELECT ITEMS FROM...") calibrated to a grid that does not exist.
MIN_TILES_PER_ROW = 4

#: How much the spacing between tile centres may vary, relative to the pitch,
#: before the "grid" is treated as coincidence.
MAX_SPACING_SPREAD = 0.25


@dataclass
class TextTile:
    """One name found in a frame, with the box it occupies."""

    text: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def centre_x(self) -> float:
        return (self.left + self.right) / 2


@dataclass
class Calibration:
    """The measured layout of one recording."""

    column_pitch: float
    row_pitch: float
    first_centre_x: float      # centre of the leftmost column
    #: y of one name row IN THE FRAME IT WAS MEASURED FROM. This is the one
    #: field that does not carry across frames: the moment the view scrolls it
    #: is stale. Averaging it over several frames — which an earlier version did
    #: — produces a number belonging to no frame at all. Re-anchor per frame
    #: with `anchor(frame)`.
    name_line_y: float
    name_height: float
    columns: int
    confidence: float          # 0..1, from how consistent the spacing was
    sample_lines: int = 0
    notes: list[str] = field(default_factory=list)

    def name_box(self, column: int, row_offset_px: float, anchor_y: float | None = None,
                 pad_x: float = 0.46, lead: float = 0.35) -> tuple[int, int, int, int]:
        """Pixel box of a name, `row_offset_px` below `anchor_y`.

        `anchor_y` MUST be the anchor measured in the frame being cropped. It
        defaults to the stored one only so a single-frame calibration works
        directly; passing the stored anchor while cropping a different frame
        puts every box on the wrong pixels, which is a silent failure — OCR
        returns junk rather than an error.

        `pad_x` widens the box either side of the column centre, because names
        are centred and long ones reach further out. `lead` starts the box
        slightly above the line so an upward wrap is not clipped.
        """
        base = self.name_line_y if anchor_y is None else anchor_y
        half_width = self.column_pitch * pad_x
        x = self.first_centre_x + column * self.column_pitch - half_width
        top = base + row_offset_px - self.name_height * lead
        return (int(round(x)), int(round(top)),
                int(round(half_width * 2)), int(round(self.name_height)))


def _ocr_words(image: np.ndarray) -> list[dict]:
    """Every word Tesseract finds in the frame, with its box."""
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        return []
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "11", "tsv"],
        input=buffer.tobytes(), capture_output=True, timeout=120)

    # Parsed by hand rather than with csv.DictReader. Tesseract's TSV is not
    # quoted, and a recognised "word" can itself contain a tab or a newline —
    # OCR on a noisy crop invents such things. DictReader then silently splices
    # rows together and hands back raw TSV as the text of a word, which flows
    # straight into the matcher as a plausible-looking string.
    lines = result.stdout.decode("utf-8", "replace").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    try:
        columns = {name: header.index(name)
                   for name in ("left", "top", "width", "height", "conf", "text")}
    except ValueError:
        return []
    expected = len(header)

    words = []
    for line in lines[1:]:
        fields = line.split("\t")
        # A row with the wrong field count is damaged, not recoverable.
        if len(fields) != expected:
            continue
        text = fields[columns["text"]].strip()
        if not text:
            continue
        try:
            confidence = float(fields[columns["conf"]])
            box = {name: int(fields[columns[name]])
                   for name in ("left", "top", "width", "height")}
        except ValueError:
            continue
        if confidence >= MIN_WORD_CONFIDENCE:
            words.append({"text": text, **box})
    return words


def _group_lines(words: list[dict]) -> list[list[dict]]:
    """Words into text lines by vertical proximity."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: w["top"])
    lines: list[list[dict]] = [[ordered[0]]]
    for word in ordered[1:]:
        if word["top"] - lines[-1][-1]["top"] <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


def _tiles_in_line(line: list[dict]) -> list[TextTile]:
    """Split one text line into tiles at the large horizontal gaps."""
    ordered = sorted(line, key=lambda w: w["left"])
    groups: list[list[dict]] = [[ordered[0]]]
    for word in ordered[1:]:
        previous = groups[-1][-1]
        if word["left"] - (previous["left"] + previous["width"]) > TILE_GAP:
            groups.append([word])
        else:
            groups[-1].append(word)

    tiles = []
    for group in groups:
        tiles.append(TextTile(
            text=" ".join(w["text"] for w in group),
            left=min(w["left"] for w in group),
            top=min(w["top"] for w in group),
            right=max(w["left"] + w["width"] for w in group),
            bottom=max(w["top"] + w["height"] for w in group),
        ))
    return tiles


def calibrate(frame_bgr: np.ndarray) -> Calibration | None:
    """Measure the grid from one still frame. None when it cannot be measured."""
    words = _ocr_words(frame_bgr)
    if len(words) < 8:
        return None

    lines = _group_lines(words)
    populated = [line for line in lines if len(line) >= 4]
    if len(populated) < 2:
        return None

    # Row pitch does NOT come from the text. Wrapped names produce a second
    # text line of their own, so the gaps between text lines are a mixture of
    # "next row" (444 px here) and "second line of the same name" (~50 px), and
    # a median over that lands on neither — measured, it returned 160.
    #
    # Edge projection has no such problem: tile borders are strictly periodic.
    # So the pitch comes from the edges and the anchor from the text, each
    # method doing what it is actually good at.
    from wft.inventory import grid as grid_module

    edges = grid_module.detect(frame_bgr)
    if edges.pitch_y <= 0 or edges.confidence < 0.15:
        return None
    row_pitch = edges.pitch_y

    # The name row is the text line carrying the most tiles: a full row of six
    # names beats any line of quantity badges or wrapped fragments.
    best_line = max(populated, key=lambda line: len(_tiles_in_line(line)))
    tiles = _tiles_in_line(best_line)
    if len(tiles) < 3:
        return None

    if len(tiles) < MIN_TILES_PER_ROW:
        return None

    centres = sorted(tile.centre_x for tile in tiles)
    centre_gaps = np.diff(centres)
    column_pitch = float(np.median(centre_gaps))
    if column_pitch <= 0:
        return None

    # Regular spacing is what distinguishes a grid from text that happens to sit
    # apart. Without this check a dialog box calibrates happily and every later
    # crop lands on nothing.
    if float(np.std(centre_gaps) / column_pitch) > MAX_SPACING_SPREAD:
        return None

    # Where column zero sits. Taking the leftmost tile found and reducing it
    # modulo the pitch is wrong: the leftmost *readable* tile is often column
    # one, and the reduction then reports a first column that does not exist.
    # Measured, that produced 16.5 px where the true centre is ~215 px, which
    # put every crop across the boundary between two tiles — OCR then returns
    # the tail of one name and the head of the next ("Alloy Adran").
    #
    # Every centre carries the same offset modulo the pitch, so the median of
    # the residues recovers it regardless of which columns happened to be read.
    residues = np.array(centres) % column_pitch
    # Guard the wrap-around: residues near 0 and near pitch are the same offset.
    if residues.max() - residues.min() > column_pitch / 2:
        residues = (residues + column_pitch / 2) % column_pitch - column_pitch / 2
    first_centre = float(np.median(residues)) % column_pitch

    reference_y = float(np.median([tile.top for tile in tiles]))
    single_line_height = float(np.median([tile.bottom - tile.top for tile in tiles]))

    # A name can wrap, so the crop must span two lines even when the sampled
    # row happened to hold only single-line names. Measured line spacing within
    # a wrapped name is close to the glyph height, so two glyph heights plus a
    # little slack covers both cases without reaching the player-visible rows
    # above or below.
    name_height = single_line_height * 2.2

    # Confidence: how tightly the observed spacings cluster. Scattered spacings
    # mean the "grid" is an artefact.
    spread = float(np.std(centre_gaps) / column_pitch) if centre_gaps.size else 1.0
    confidence = max(0.0, min(1.0, 1.0 - spread))

    columns = INVENTORY_COLUMNS

    notes = []
    if len(tiles) < columns:
        notes.append(f"{columns - len(tiles)} of {columns} columns carried no "
                     f"readable text in the sampled row")

    # The pitch has to be consistent with six columns fitting in the frame. If
    # it is not, the measurement found something other than the item grid.
    if column_pitch * columns > frame_bgr.shape[1] * 1.05:
        return None

    return Calibration(
        column_pitch=column_pitch, row_pitch=row_pitch,
        first_centre_x=first_centre, name_line_y=reference_y,
        name_height=name_height, columns=columns,
        confidence=confidence, sample_lines=len(populated), notes=notes)


def calibrate_stable(frames: list[np.ndarray]) -> Calibration | None:
    """Calibrate across several frames, taking the median of what succeeded.

    One frame can be caught mid-scroll or behind a tooltip. Agreement across
    frames is what makes the numbers trustworthy; disagreement is reported
    rather than averaged into something plausible-looking.
    """
    results = [c for c in (calibrate(frame) for frame in frames) if c is not None]
    if not results:
        return None

    def median_of(attribute: str) -> float:
        return float(np.median([getattr(c, attribute) for c in results]))

    column_pitch = median_of("column_pitch")
    # name_line_y is taken from the LAST successful frame rather than a median,
    # and callers are expected to re-anchor anyway. A median of anchors from
    # different scroll positions describes no frame that exists.
    spread = float(np.std([c.column_pitch for c in results]))
    notes = [n for c in results for n in c.notes]
    if spread > column_pitch * 0.05:
        notes.append(f"column pitch varied by {spread:.0f}px across frames — "
                     f"the recording may be scaled inconsistently")

    return Calibration(
        column_pitch=column_pitch,
        row_pitch=median_of("row_pitch"),
        first_centre_x=median_of("first_centre_x"),
        name_line_y=results[-1].name_line_y,
        name_height=median_of("name_height"),
        columns=int(median_of("columns")),
        confidence=median_of("confidence"),
        sample_lines=sum(c.sample_lines for c in results),
        notes=sorted(set(notes)),
    )


def anchor(frame_bgr: np.ndarray, reference: Calibration) -> float | None:
    """Where a name row sits in *this* frame.

    The spacings from `calibrate_stable` hold for a whole recording; the anchor
    does not survive a single scroll step. This finds the current one by reading
    the frame again and taking the text line that carries the most tiles.

    Returns None when nothing readable is found — mid-scroll frames and frames
    behind a tooltip do happen, and inventing an anchor for them would put every
    crop on the wrong pixels.
    """
    words = _ocr_words(frame_bgr)
    if len(words) < 6:
        return None
    lines = [line for line in _group_lines(words) if len(line) >= 3]
    if not lines:
        return None
    best = max(lines, key=lambda line: len(_tiles_in_line(line)))
    if len(_tiles_in_line(best)) < 2:
        return None
    return float(np.median([w["top"] for w in best]))


def rows_in_frame(frame_height: int, anchor_y: float, row_pitch: float,
                  name_height: float) -> list[float]:
    """Offsets, relative to `anchor_y`, of every name row fully inside the frame."""
    offsets = []
    step = -row_pitch * int(anchor_y // row_pitch + 2)
    while anchor_y + step < frame_height:
        top = anchor_y + step
        if top >= 0 and top + name_height <= frame_height:
            offsets.append(step)
        step += row_pitch
    return offsets
