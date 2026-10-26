"""Finding the tile grid in a frame.

Hardcoding the grid was tempting and would have been wrong. Measured on a real
recording, the tile spacing came out at 0.1615 of the width where a full-window
screenshot gives 0.109 — because the recording captured only the item area,
without the header and margins. Any fixed set of fractions works exactly for the
one capture it was measured on.

So the grid is measured per recording. The method is deliberately dull: tile
borders are the only strong vertical and horizontal structure in the image, so
projecting edge energy onto each axis produces a periodic signal, and the period
is the tile pitch. No template matching, no assumptions about tile appearance,
nothing that a UI theme could break.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Grid:
    """A tile grid in pixel coordinates of the analysed frame."""

    x0: float           # left edge of the first column
    y0: float           # top edge of the first row, before any scrolling
    pitch_x: float
    pitch_y: float
    columns: int
    confidence: float   # 0..1, how periodic the signal actually was

    def tile_box(self, column: int, row: int, scroll: float = 0.0) -> tuple[int, int, int, int]:
        """Pixel box (x, y, w, h) of one tile, shifted by the current scroll offset."""
        x = self.x0 + column * self.pitch_x
        y = self.y0 + row * self.pitch_y - scroll
        return int(round(x)), int(round(y)), int(round(self.pitch_x)), int(round(self.pitch_y))

    def visible_rows(self, frame_height: int) -> float:
        return frame_height / self.pitch_y


def _edge_profile(gray: np.ndarray, axis: int) -> np.ndarray:
    """Edge energy projected onto one axis.

    Sobel in the direction across the axis, absolute value, summed. Tile borders
    run the full width or height of the grid, so they dominate this sum while
    item artwork averages out.
    """
    kernel = (1, 0) if axis == 1 else (0, 1)
    edges = np.abs(cv2.Sobel(gray, cv2.CV_32F, kernel[0], kernel[1], ksize=3))
    profile = edges.sum(axis=axis)
    profile -= profile.mean()
    return profile


def _dominant_period(profile: np.ndarray, low: int, high: int) -> tuple[float, float]:
    """The strongest repeat length in a 1-D signal, via autocorrelation.

    Returns (period, confidence). Confidence is the peak height relative to the
    signal's own energy, so a frame with no grid at all scores near zero rather
    than returning a confident nonsense value.
    """
    if profile.size < high * 2:
        return 0.0, 0.0

    signal = profile - profile.mean()
    correlation = np.correlate(signal, signal, mode="full")[signal.size - 1:]
    if correlation[0] <= 0:
        return 0.0, 0.0
    correlation = correlation / correlation[0]

    window = correlation[low:high]
    if window.size == 0:
        return 0.0, 0.0
    offset = int(np.argmax(window))
    period = float(low + offset)
    confidence = float(window[offset])

    # Refine to sub-pixel with a parabola through the peak and its neighbours.
    index = low + offset
    if 0 < index < correlation.size - 1:
        before, peak, after = correlation[index - 1], correlation[index], correlation[index + 1]
        denominator = before - 2 * peak + after
        if denominator != 0:
            period += 0.5 * (before - after) / denominator

    return period, max(0.0, min(1.0, confidence))


def _first_edge(profile: np.ndarray, period: float) -> float:
    """Where the first grid line sits, given the period.

    Correlating the profile against a comb of the known period and taking the
    best shift is more robust than looking for the first peak, which noise can
    invent anywhere.
    """
    if period <= 1:
        return 0.0
    length = profile.size
    positions = np.arange(length)
    best_shift, best_score = 0.0, -np.inf
    for shift in np.arange(0, period, max(0.5, period / 60)):
        comb = np.cos(2 * np.pi * (positions - shift) / period)
        score = float(np.dot(profile, comb))
        if score > best_score:
            best_score, best_shift = score, float(shift)
    return best_shift


def detect(frame_bgr: np.ndarray, min_pitch: int = 60, max_pitch: int = 700) -> Grid:
    """Measure the tile grid in one frame.

    `min_pitch` and `max_pitch` bound the search in pixels. The defaults span
    everything from a small window to a 4K capture; they exist to keep
    autocorrelation from locking onto texture detail or onto the frame itself.
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    vertical = _edge_profile(gray, axis=0)    # varies along x -> column pitch
    horizontal = _edge_profile(gray, axis=1)  # varies along y -> row pitch

    pitch_x, confidence_x = _dominant_period(vertical, min_pitch, min(max_pitch, gray.shape[1] // 2))
    pitch_y, confidence_y = _dominant_period(horizontal, min_pitch, min(max_pitch, gray.shape[0] // 2))

    x0 = _first_edge(vertical, pitch_x) if pitch_x else 0.0
    y0 = _first_edge(horizontal, pitch_y) if pitch_y else 0.0
    columns = int(gray.shape[1] // pitch_x) if pitch_x else 0

    return Grid(x0, y0, pitch_x, pitch_y, columns,
                confidence=min(confidence_x, confidence_y))


def detect_stable(frames: list[np.ndarray], min_confidence: float = 0.15) -> Grid | None:
    """Measure across several frames and take the median.

    A single frame can be measured while the view is mid-scroll or while an
    overlay is up. Agreement across frames is what makes the result
    trustworthy — and disagreement is a signal in itself, so the caller gets
    None rather than a plausible-looking average of nonsense.
    """
    grids = [detect(frame) for frame in frames]
    usable = [g for g in grids if g.confidence >= min_confidence and g.pitch_y > 0]
    if not usable:
        return None

    def median(values: list[float]) -> float:
        return float(np.median(values))

    pitch_y = median([g.pitch_y for g in usable])
    pitch_x = median([g.pitch_x for g in usable])

    # Row offset is meaningless across frames while scrolling; only the pitch
    # and the column layout carry over. y0 is re-derived per frame from the
    # tracked scroll offset instead.
    return Grid(
        x0=median([g.x0 for g in usable]),
        y0=median([g.y0 for g in usable]),
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        columns=int(median([g.columns for g in usable])),
        confidence=median([g.confidence for g in usable]),
    )
