"""Is the reward screen still up? A check on its header, for the reward overlay.

The reward screen has a header, "VOID FISSURE / REWARDS", whose second word is
yellow (254, 253, 4 in the blue interface theme; a dark gold in the light one,
both measured) on a background that moves.
The overlay must end with the screen, and a timer cannot say when that is (a
player who reads the screen at 5 seconds left would see the overlay 10 seconds
too long). So the tracker keeps a *mask* of the yellow pixels of that word from
the picture of the reading and, every half second, grabs only the strip of the
header and asks which share of the mask's pixels is still yellow.

Everything is a fraction of the game window, as everywhere else. Arrays are BGR,
the form `wft.capture.window.to_bgr` gives.
"""
from __future__ import annotations

#: The strip that holds the yellow word, as fractions of the window (x, y, w, h).
#: Measured on a 4K corpus: the word spans x 0.331 to 0.430 and y
#: 0.045 to 0.079; the strip leaves a margin around it.
REGION = (0.325, 0.040, 0.115, 0.050)

#: A pixel counts as header colour when hue (OpenCV scale, 0 to 180), saturation
#: and value fall in these ranges. A hue range rather than the one yellow, because
#: the interface has a light theme in which the same word is dark gold; both pass.
HUE = (12, 36)
SATURATION_MIN = 170
VALUE_MIN = 110

#: Fewest header pixels per 1,000,000 pixels of window for a picture to count as
#: having a header at all. Between the 200 per 8,294,400 of the strongest
#: false picture and the 1,313 of the weakest true one.
MIN_MASK_PER_MILLION = 90

#: Share of the mask's pixels that must still be yellow for the header to count
#: as there. Measured on real captures.
PRESENT_SHARE = 0.5


def yellow(strip):
    """Boolean array: which pixels of a BGR strip have the header's colour."""
    import cv2

    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    return ((hsv[..., 0] >= HUE[0]) & (hsv[..., 0] <= HUE[1])
            & (hsv[..., 1] >= SATURATION_MIN) & (hsv[..., 2] >= VALUE_MIN))


def crop(image, region=REGION):
    """The strip of a whole-window BGR picture that `region` names."""
    height, width = image.shape[:2]
    x, y, w, h = region
    x0, y0 = int(x * width), int(y * height)
    x1, y1 = max(x0 + 1, int((x + w) * width)), max(y0 + 1, int((y + h) * height))
    return image[y0:y1, x0:x1]


def reference_mask(image, region=REGION):
    """The yellow mask of the header in a whole-window picture, or None when the
    picture has no recognisable header (too little yellow)."""
    strip = crop(image, region)
    mask = yellow(strip)
    window_pixels = image.shape[0] * image.shape[1]
    if int(mask.sum()) * 1_000_000 < MIN_MASK_PER_MILLION * window_pixels:
        return None
    return mask


def share_present(mask, strip) -> float:
    """The share of the mask's pixels that are yellow in `strip` (same strip of a
    later picture). A strip of another size is compared after scaling the mask
    to it, nearest neighbour."""
    import numpy

    total = int(mask.sum())
    if total == 0:
        return 0.0
    if strip.shape[:2] != mask.shape:
        rows = (numpy.arange(strip.shape[0]) * mask.shape[0] // strip.shape[0])
        cols = (numpy.arange(strip.shape[1]) * mask.shape[1] // strip.shape[1])
        mask = mask[rows][:, cols]
        total = int(mask.sum())
        if total == 0:
            return 0.0
    return float((yellow(strip) & mask).sum()) / total


def is_present(mask, strip) -> bool:
    return share_present(mask, strip) >= PRESENT_SHARE


def grab_strip(placement, region=REGION):
    """The header strip of the live game window, BGR. `placement` is a
    `wft.capture.window.Placement`. Raises `CaptureError` when the window cannot
    be read (it is gone, for one)."""
    from wft.capture import window as cap

    return cap.to_bgr(cap.grab(placement.as_window(), region))
