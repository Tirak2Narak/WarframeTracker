"""Choosing which frames of a recording to look at.

Sampling at a fixed rate is the obvious approach and the wrong one. The user
scrolls in flicks: bursts of up to 237 px between consecutive frames, separated
by genuine pauses, and the pattern differs between recordings. A fixed rate then
spends most of its effort on near-identical frames during pauses and skips tiles
during bursts.

Sampling by *movement* — keep a frame once the view has shifted more than X
pixels since the last kept one — makes the coverage independent of how fast
anyone scrolled. Measured on a real recording, at comparable frame counts:

    every 4th frame     670 frames, gap spread CV 1.42, largest gap 691 px
    46 px of movement   813 frames, gap spread CV 0.52, gaps clustered at 46

and the movement-based selection had the lower spread at every threshold tried.
The practical consequence is that the number of readings per tile stays roughly
constant across recordings, which is what the majority vote depends on.

Two cheap facts make this affordable: the movement is purely vertical
(horizontal standard deviation 0.02 px), and there is no motion blur to
compensate for (sharpness against speed correlates at -0.014).
"""
from __future__ import annotations

import math

from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np

#: Mean absolute difference below which two frames count as the same picture.
#: Measured across three recordings, the histogram has an empty bucket between
#: 2 and 3 on the 0-255 scale, separating "identical" from "changed" — so this
#: threshold sits in a gap rather than on a slope.
SAME_FRAME_MAD = 2.5

#: How far the view must move before another frame is worth reading, in pixels
#: of the source. Roughly a tenth of a tile row, giving about ten readings per
#: tile at the measured row pitch.
#: Raised from 46 after measuring what it buys. At 46px against a 444px row
#: pitch a tile is seen about nine times; three to five is enough for a majority
#: when the failure mode is static artwork noise rather than random error.
#: Halving the samples halves the OCR pass, which is the expensive one.
MOVEMENT_STEP = 92.0

#: Fraction of the frame trimmed before measuring, to exclude the static header
#: and footer. Measured constant regions: top 0-1%, bottom 1-9%, sides <2%.
TRIM_TOP, TRIM_BOTTOM, TRIM_SIDES = 0.03, 0.09, 0.02

#: The correlation is limited to the grid, which is the only part that moves.
#: Everything to its right is the detail pane: it holds still while the grid
#: scrolls, and static content in a phase correlation puts a peak at zero. On
#: the small steps between consecutive frames that peak sits inside the
#: sub-pixel centroid of the real one and drags every measurement towards zero
#: - an error too small to see in one step and cumulative over a recording, so
#: the virtual page comes out compressed against the screen.
#:
#: Measured over 240 consecutive frames: 3557px of scroll with the pane
#: included against 3589px without, or 0.9% of the page lost. Two different
#: crops of the grid alone agree to within a pixel, so the number without the
#: pane is the trustworthy one.
MEASURE_WIDTH = 0.68


@dataclass
class Sample:
    """One frame worth reading, with where it sits in the scroll."""

    index: int              # frame number in the source
    image: np.ndarray       # BGR
    scroll: float           # accumulated vertical offset since the first frame
    step: float             # movement since the previously kept frame
    #: Whether this came from a still picture rather than a recording. A
    #: screenshot is seen exactly once by definition, which a rule written for
    #: video frames reads as "not seen enough to trust".
    still: bool = False
    #: Which file in the folder this came from. Two screenshots of the same
    #: shelf are two pages of the virtual sheet, so the same tile appears twice
    #: - and only the source can tell that from two copies of the item.
    source: int = 0


#: The correlation runs on the frame shrunk by this factor, and the measured
#: shift is scaled back up. Phase correlation on a full 4K frame costs 90ms and
#: runs on every changed frame — several minutes over a recording, for a number
#: that only needs to be accurate to a pixel or two. At quarter size it costs
#: about 6ms and the answer is the same to within a fraction of a tile.
CORRELATION_SCALE = 4


def _measurable(frame: np.ndarray) -> np.ndarray:
    """Grayscale centre of the frame: the part that actually scrolls.

    Including the static header would anchor the correlation and report no
    movement at all, which is a silent failure — every frame then looks like a
    pause. The detail pane on the right is the same failure in miniature: it
    does not stop the measurement, it shrinks it, and the shortfall accumulates
    into a virtual page that is a percent shorter than the screen it was read
    from.
    """
    height, width = frame.shape[:2]
    top = int(height * TRIM_TOP)
    bottom = height - int(height * TRIM_BOTTOM)
    left = int(width * TRIM_SIDES)
    right = min(width - left, int(width * MEASURE_WIDTH))
    region = frame[top:bottom, left:right]
    grey = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    if CORRELATION_SCALE > 1:
        grey = cv2.resize(grey, None, fx=1 / CORRELATION_SCALE, fy=1 / CORRELATION_SCALE,
                          interpolation=cv2.INTER_AREA)
    return grey.astype(np.float32)


def shift_between(previous: np.ndarray, current: np.ndarray) -> tuple[float, float]:
    """Vertical shift in source pixels and a 0..1 confidence.

    Measured on the reduced image and scaled back, so callers see offsets in the
    coordinates of the original frame.

    The awkward reduced size (476x634 on a 4K capture) is fine as it is:
    phaseCorrelate pads to the optimal DFT size internally - verified
    bit-identical against pre-padded input, and within 0.3ms of it.
    """
    (_, shift_y), response = cv2.phaseCorrelate(previous, current)
    return float(shift_y) * CORRELATION_SCALE, float(response)


def mean_absolute_difference(previous: np.ndarray, current: np.ndarray) -> float:
    return float(np.mean(np.abs(previous - current)))


def total_frames(path: str) -> int:
    """Frame count from the container. Approximate, but enough for progress."""
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        return 0
    count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    capture.release()
    return count


def sample(path: str, movement_step: float = MOVEMENT_STEP,
           same_frame_mad: float = SAME_FRAME_MAD,
           progress=None, cancel=None) -> Iterator[Sample]:
    """Walk a recording, yielding frames spaced by movement rather than time.

    Frames whose difference from the last kept one is below `same_frame_mad`
    are skipped before any correlation is computed — during a pause that is most
    of them, and the difference is far cheaper than the correlation.
    """
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise FileNotFoundError(path)

    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    ok, frame = capture.read()
    if not ok:
        capture.release()
        return

    previous = _measurable(frame)
    scroll = 0.0
    pending = 0.0
    index = 0
    #: The last frame that movement was measured on and that was not kept.
    tail: tuple[int, np.ndarray, float] | None = None
    yield Sample(index=0, image=frame, scroll=0.0, step=0.0)

    while True:
        ok, frame = capture.read()
        if not ok:
            break
        index += 1
        if cancel is not None and cancel():
            break
        if progress and index % 50 == 0:
            progress(index, total)

        current = _measurable(frame)

        # Cheap rejection first: an unchanged picture cannot have moved.
        if mean_absolute_difference(previous, current) < same_frame_mad:
            previous = current
            continue

        shift_y, response = shift_between(previous, current)
        previous = current
        if response < 0.05:
            # Correlation found nothing to lock onto — a transition, an overlay,
            # a scene change. Better to skip than to record a wrong offset.
            continue

        scroll += -shift_y          # negative shift means content moved up
        pending += abs(shift_y)
        tail = (index, frame, scroll)
        if pending >= movement_step:
            yield Sample(index=index, image=frame, scroll=scroll, step=pending)
            pending = 0.0
            tail = None

    # Whatever movement had accumulated when the recording ended, below the
    # step that would have kept a frame, used to be thrown away - and with it
    # the last frame. That is the worst frame to lose: a person scrolls to the
    # bottom of a list and then stops recording, so the discarded frame is the
    # only one showing the final row.
    #
    # Measured on a Warframes recording: Yareli Prime, Yareli and
    # Zephyr were in the last frame, read correctly when that frame was handed
    # over on its own, and missing from every scan of the recording.
    if tail is not None:
        index, frame, scroll = tail
        yield Sample(index=index, image=frame, scroll=scroll, step=pending)

    capture.release()


#: Image files a folder import will pick up, lower-cased.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

#: Video files a folder import will pick up, lower-cased.
VIDEO_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".avi")

#: How much of a screen height two consecutive screenshots may be apart and
#: still be stitched into one page. The grid is periodic, so phase correlation
#: on a large jump can lock onto the wrong row and report a shift that is a
#: whole tile out. Beyond this the pictures are treated as separate pages
#: instead, which costs deduplication and buys never inventing an offset.
MAX_STITCH_FRACTION = 0.75

#: Correlation strength below which two screenshots are not the same page.
#: `sample` uses the same figure for a video, where it means a transition; here
#: it usually means a different category tab.
MIN_STITCH_RESPONSE = 0.05


def media_files(folder: str) -> list[str]:
    """Every recording and screenshot in a folder, in name order.

    Both kinds, because a folder is how someone captures one category at a
    time and there is no reason they would all be the same kind - a tab that
    fits on one screen is a screenshot, a long one is a recording.

    Name order rather than modification time: screen-capture tools name by
    timestamp, and a folder copied between machines keeps its names while its
    mtimes are whatever the copy made them.
    """
    import os

    wanted = IMAGE_SUFFIXES + VIDEO_SUFFIXES
    entries = [os.path.join(folder, name) for name in sorted(os.listdir(folder))]
    return [path for path in entries
            if os.path.isfile(path)
            and os.path.splitext(path)[1].lower() in wanted]


def image_files(folder: str) -> list[str]:
    """Only the images in a folder, in name order."""
    import os

    return [path for path in media_files(folder)
            if os.path.splitext(path)[1].lower() in IMAGE_SUFFIXES]


def is_video(path: str) -> bool:
    import os

    return os.path.splitext(path)[1].lower() in VIDEO_SUFFIXES


def sample_folder(paths: list[str], movement_step: float = MOVEMENT_STEP,
                  cancel=None, on_file=None) -> Iterator[Sample]:
    """Yield samples from a folder holding recordings, screenshots or both.

    Each file is its own page. Two captures of different tabs share nothing, so
    stitching across a file boundary could only invent an offset - the gap
    between files is made wide enough that no tile from one can land in the
    same grid row as a tile from the next.

    Inside a file the usual rules apply: a recording is walked by movement, and
    consecutive screenshots are stitched where they overlap and separated where
    they do not.

    `on_file` is called with (index, path, count) as each file starts, so a
    caller can say which of five captures it is on rather than showing one bar
    that restarts.
    """
    import os

    from wft.inventory import pagemodel as pm

    offset = 0.0
    consumed = 0
    #: The tile pitch, measured once off the first frame that shows enough of
    #: the grid to answer. One monitor, one interface, one pitch - measuring it
    #: per file would be honest and pointless, per frame would buy jitter.
    #: Local to this walk, so a later scan of a different resolution measures
    #: its own.
    pitch: float | None = None
    for position, path in enumerate(paths):
        if cancel is not None and cancel():
            break
        if on_file:
            on_file(position, path, len(paths))

        highest = offset
        video = is_video(path)
        if video:
            inner = sample(path, movement_step=movement_step, cancel=cancel)
        else:
            inner = sample_images([path], cancel=cancel)

        height = 0
        seen = 0
        #: How far this capture's own tile boundaries sit from a whole pitch,
        #: measured on its first frame and subtracted from every scroll in it.
        #:
        #: Without this each file lands on the virtual page with its own row
        #: phase - a capture starts wherever the user had scrolled to, and the
        #: gap between files is not a whole number of rows either. The grid is
        #: then fitted with a single phase across five combs that have five
        #: different ones, which is not a noisy problem but an unsolvable one:
        #: the winner is a pitch that reconciles none of them.
        #:
        #: The symptom was every stack count in a folder import coming back as
        #: 1. The badge window is measured in fractions of a tile height, so a
        #: pitch that is 10% out moves it by most of a badge, the tick is
        #: clipped, and "no badge" means "does not stack" by design.
        align = 0.0
        measured_here = False
        for local in inner:
            # Numbered across the whole folder, and still counting source
            # frames. The index does two jobs: it names the file a frame is
            # written to, so two captures starting at zero would overwrite each
            # other, and it is what the progress bar measures against the total
            # frame count. Numbering the kept samples instead satisfied the
            # first and broke the second - a handful of kept frames against
            # thousands of source ones leaves the bar reading zero.
            seen = local.index + 1
            height = max(height, local.image.shape[0])
            if not measured_here:
                measured_here = True
                if pitch is None:
                    found = pm.measure_pitch(local.image)
                    pitch = float(found[0]) if found else 0.0
                if pitch:
                    phase = pm.gap_phase(local.image, pitch)
                    align = float(phase) if phase is not None else 0.0
            scroll = offset + local.scroll - align
            highest = max(highest, scroll)
            yield Sample(index=consumed + local.index, image=local.image,
                         scroll=scroll, step=local.step, still=local.still,
                         source=position)

        # A whole number of rows, so the alignment survives the gap.
        span = max(height, 1) * 2.0
        offset = (highest + math.ceil(span / pitch) * pitch if pitch
                  else highest + span)
        # `max` because a container's header lies: webm and mkv often report 0
        # or a wrong count, and a count that does not advance past what was
        # actually read makes the next file reuse these indices — two frames
        # written to one file on disk, read back with two different scroll
        # offsets, tiles landing twice in the wrong rows.
        consumed += max(total_frames(path), seen) if video else max(seen, 1)


def sample_images(paths: list[str], cancel=None) -> Iterator[Sample]:
    """Yield screenshots as samples, stitched into pages where they overlap.

    A folder of screenshots is what the video pass produces anyway - pictures
    on disk - so the rest of the pipeline needs nothing new. What it does need
    is the scroll offset of each picture, and unlike a video there is no
    guarantee that two consecutive ones overlap at all.

    So the offset is measured where it can be and invented nowhere. When the
    correlation is weak, or the implied jump is most of a screen, the next
    picture starts a new page a long way down the virtual one. Tiles from
    different pages then cannot be mistaken for each other, which is the right
    failure: two screenshots of the same shelf counted twice is a wrong number,
    but two screenshots stitched at a wrong offset is a wrong number in every
    row of both.
    """
    previous = None
    scroll = 0.0
    for index, path in enumerate(paths):
        if cancel is not None and cancel():
            break
        frame = cv2.imread(path)
        if frame is None:
            continue
        current = _measurable(frame)

        if previous is not None:
            shift_y, response = shift_between(previous, current)
            limit = frame.shape[0] * MAX_STITCH_FRACTION
            if response >= MIN_STITCH_RESPONSE and abs(shift_y) <= limit:
                scroll += -shift_y
            else:
                # A page of its own. The gap is a whole screen plus the tallest
                # plausible tile, so nothing from the previous picture can land
                # in the same grid row as anything in this one.
                scroll += frame.shape[0] * 2.0

        previous = current
        yield Sample(index=index, image=frame, scroll=scroll, step=0.0,
                     still=True)


def probe(path: str, count: int = 6, search_window: int = 90) -> list[np.ndarray]:
    """Still frames spread across the recording, for calibration.

    Deliberately *still* ones. Taking frames at fixed positions lands them
    mid-scroll as often as not, and calibrating on a moving picture finds fewer
    readable names — measured, that dropped a 4K recording to four readable
    columns out of nine and the grid came out wrong.

    From each sampling point, this walks forward until two consecutive frames
    are nearly identical, which is the definition of a pause here.
    """
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise FileNotFoundError(path)

    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    frames: list[np.ndarray] = []
    for position in range(1, count + 1):
        start = int(total * position / (count + 1))
        capture.set(cv2.CAP_PROP_POS_FRAMES, start)
        ok, previous = capture.read()
        if not ok:
            continue
        previous_measured = _measurable(previous)

        chosen = previous
        for _ in range(search_window):
            ok, frame = capture.read()
            if not ok:
                break
            measured = _measurable(frame)
            if mean_absolute_difference(previous_measured, measured) < SAME_FRAME_MAD:
                chosen = frame          # two frames agree: the view is at rest
                break
            previous_measured = measured
        frames.append(chosen)

    # More candidates than asked for, spread wider: a still frame is not
    # necessarily an inventory frame — menus and dialogs are still too — so the
    # caller needs alternatives to fall back on.
    for extra in (0.15, 0.35, 0.55, 0.75, 0.92):
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(total * extra))
        ok, frame = capture.read()
        if ok:
            frames.append(frame)

    capture.release()
    return frames
