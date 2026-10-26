"""Finding and grabbing the game window.

Warframe runs under Proton, which means it is an X11 client on XWayland even on
a Wayland desktop. That makes its window addressable by XID, and its pixels
readable with a plain `XGetImage` — no portal, no D-Bus, no helper process, and
no screenshot file on disk.

The important property is not speed but that everything is **window-relative**.
Regions are fractions of the window, so they survive a resolution change, a move
to another monitor, and a different monitor layout entirely. The previous
attempt worked in global desktop coordinates across four monitors and spent a
lot of effort there (`RELIC-HELPER.md:299-387`).

Grabbing the root window instead is not an option here: under XWayland it fails
with BadMatch, which is what rules out `python-mss`.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass

_x11 = ctypes.CDLL(ctypes.util.find_library("X11"))

ZPIXMAP = 2
ALL_PLANES = 0xFFFFFFFF
IS_VIEWABLE = 2


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int), ("data", ctypes.POINTER(ctypes.c_char)),
        ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int), ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
    ]


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong),
        ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long), ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _ClassHint(ctypes.Structure):
    # Held as void pointers, not c_char_p, on purpose. Reading a c_char_p field
    # makes ctypes copy it into a Python bytes object and discard the original
    # pointer, so XFree would then be handed the wrong address — which aborts
    # with "free(): invalid size" rather than failing quietly.
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


# Every signature is declared, including the ones returning nothing. An
# undeclared ctypes function defaults to a 32-bit int for pointer arguments,
# which silently truncates a Display* and segfaults — that cost a debugging
# round here, so the list is exhaustive on purpose.
_x11.XOpenDisplay.restype = ctypes.c_void_p
_x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
_x11.XCloseDisplay.restype = ctypes.c_int
_x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
_x11.XDefaultRootWindow.restype = ctypes.c_ulong
_x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
_x11.XGetImage.restype = ctypes.POINTER(_XImage)
_x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                           ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
_x11.XDestroyImage.restype = ctypes.c_int
_x11.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
_x11.XGetWindowAttributes.restype = ctypes.c_int
_x11.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                      ctypes.POINTER(_XWindowAttributes)]
_x11.XQueryTree.restype = ctypes.c_int
_x11.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                            ctypes.POINTER(ctypes.c_uint)]
_x11.XFetchName.restype = ctypes.c_int
_x11.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                            ctypes.POINTER(ctypes.c_void_p)]
_x11.XGetClassHint.restype = ctypes.c_int
_x11.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_ClassHint)]
_x11.XFree.restype = ctypes.c_int
_x11.XFree.argtypes = [ctypes.c_void_p]
_x11.XSetErrorHandler.restype = ctypes.c_void_p
_x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
_x11.XSync.restype = ctypes.c_int
_x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
_x11.XTranslateCoordinates.restype = ctypes.c_int
_x11.XTranslateCoordinates.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_ulong)]

# Xlib's default error handler calls exit() on a protocol error, which would
# take the whole application down over a window that vanished mid-grab. Ours
# records the error so the caller can fall through to another capture method.
_x11.XQueryPointer.restype = ctypes.c_int
_x11.XQueryPointer.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
    ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]

_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_last_error: list[int] = []


def _on_x_error(display, event) -> int:
    _last_error.append(1)
    return 0


_error_handler = _ERROR_HANDLER_TYPE(_on_x_error)
_x11.XSetErrorHandler(ctypes.cast(_error_handler, ctypes.c_void_p))


@dataclass
class Window:
    xid: int
    width: int
    height: int
    wm_class: str
    title: str
    depth: int = 24


@dataclass(frozen=True)
class Placement:
    """Where a window is on the desktop: its origin in root-window coordinates
    and its size. What an overlay needs to lie exactly over it."""

    xid: int
    x: int
    y: int
    width: int
    height: int

    def as_rect(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.width, "h": self.height}

    def as_window(self) -> "Window":
        """The same window as a `Window`, for `grab`."""
        return Window(self.xid, self.width, self.height, "", "")


def placement(window: Window, display=None) -> Placement:
    """The origin of `window` in root-window coordinates (`XTranslateCoordinates`).

    `Window` itself carries no position: a window's own attributes give it
    relative to its parent, which under a window manager is a frame. Asking the
    server to translate the window's (0, 0) to the root says where its pixels
    really are, which is what a window drawn over it must match.
    """
    own_display = display is None
    display = display or _open_display()
    try:
        root = _x11.XDefaultRootWindow(display)
        x, y = ctypes.c_int(), ctypes.c_int()
        child = ctypes.c_ulong()
        _last_error.clear()
        ok = _x11.XTranslateCoordinates(display, window.xid, root, 0, 0,
                                        ctypes.byref(x), ctypes.byref(y),
                                        ctypes.byref(child))
        _x11.XSync(display, 0)
        if not ok or _last_error:
            raise CaptureError(f"XTranslateCoordinates failed for window 0x{window.xid:x}")
        return Placement(window.xid, x.value, y.value, window.width, window.height)
    finally:
        if own_display:
            _x11.XCloseDisplay(display)


def pointer_position(display=None) -> tuple[int, int]:
    """Where the mouse pointer is, in root-window coordinates (`XQueryPointer`).

    The same coordinates as `Placement.x/y`, so a point can be taken relative to
    the game window. Reads the position only.
    """
    own_display = display is None
    display = display or _open_display()
    try:
        root = _x11.XDefaultRootWindow(display)
        root_return, child = ctypes.c_ulong(), ctypes.c_ulong()
        root_x, root_y = ctypes.c_int(), ctypes.c_int()
        win_x, win_y = ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        _last_error.clear()
        ok = _x11.XQueryPointer(display, root, ctypes.byref(root_return),
                                ctypes.byref(child), ctypes.byref(root_x),
                                ctypes.byref(root_y), ctypes.byref(win_x),
                                ctypes.byref(win_y), ctypes.byref(mask))
        _x11.XSync(display, 0)
        if not ok or _last_error:
            raise CaptureError("XQueryPointer failed")
        return root_x.value, root_y.value
    finally:
        if own_display:
            _x11.XCloseDisplay(display)


@dataclass
class Frame:
    """A captured region, as raw BGRX/BGR bytes plus the geometry to read it."""

    width: int
    height: int
    stride: int
    bits_per_pixel: int
    data: bytes

    def uniform(self, sample: int = 4096) -> bool:
        """Whether the grab came back blank.

        A window that is not actually rendering — or a compositor path that
        hands back an empty buffer — yields a single repeated colour. Catching
        that here is what lets the caller fall through to another method
        instead of running OCR on nothing.
        """
        step = max(1, self.bits_per_pixel // 8)
        probe = self.data[: sample * step : step]
        return len(set(probe)) <= 1


class CaptureError(RuntimeError):
    pass


def _open_display():
    display = _x11.XOpenDisplay(None)
    if not display:
        raise CaptureError("cannot open X display (is DISPLAY set?)")
    return display


def list_windows(display=None) -> list[Window]:
    """Every viewable top-level window, with its class and title."""
    own_display = display is None
    display = display or _open_display()
    try:
        root = _x11.XDefaultRootWindow(display)
        parent = ctypes.c_ulong()
        returned_root = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        _x11.XQueryTree(display, root, ctypes.byref(returned_root), ctypes.byref(parent),
                        ctypes.byref(children), ctypes.byref(count))

        found: list[Window] = []
        for index in range(count.value):
            xid = children[index]
            attributes = _XWindowAttributes()
            if not _x11.XGetWindowAttributes(display, xid, ctypes.byref(attributes)):
                continue
            if attributes.map_state != IS_VIEWABLE or attributes.width < 200:
                continue
            # XWayland exposes a container spanning the whole virtual desktop
            # (here 9840x3840) with depth 0 and no class. It is not a drawable
            # window: XGetImage on it fails with BadMatch. That container is
            # also what a root-window grab lands on, which is why python-mss
            # cannot work in this session.
            if attributes.depth == 0:
                continue

            hint = _ClassHint()
            wm_class = ""
            if _x11.XGetClassHint(display, xid, ctypes.byref(hint)):
                source = hint.res_class or hint.res_name
                if source:
                    wm_class = ctypes.string_at(source).decode("utf-8", "replace")
                # Xlib allocated both strings and expects the caller to free them.
                for pointer in (hint.res_name, hint.res_class):
                    if pointer:
                        _x11.XFree(pointer)

            name = ctypes.c_void_p()
            title = ""
            if _x11.XFetchName(display, xid, ctypes.byref(name)) and name.value:
                title = ctypes.string_at(name.value).decode("utf-8", "replace")
                _x11.XFree(name.value)

            found.append(Window(xid, attributes.width, attributes.height,
                                wm_class, title, attributes.depth))
        if children:
            _x11.XFree(ctypes.cast(children, ctypes.c_void_p).value)
        return found
    finally:
        if own_display:
            _x11.XCloseDisplay(display)


#: The tracker's own window class. "warframe", the sensible setting for the
#: game, is a substring of it, so wherever Qt runs the tracker as an X11 window
#: (an X11 session, or QT_QPA_PLATFORM=xcb) it would otherwise count as the game:
#: `wf doctor` reported it as the game window while the game was closed.
OWN_CLASS = "warframe-tracker"


def is_game(wm_class: str, title: str, window_class: str) -> bool:
    """Whether a window with this class and title is the game.

    Matched case-insensitively on class or title. A substring rather than an
    exact class on purpose: the class Proton reports has varied between
    versions, and the configured value is meant to be something the user can
    read off `xprop` and paste in. Shared by find_game() and the hotkey's focus
    check, so the two cannot disagree about what the game is.
    """
    if OWN_CLASS in wm_class.lower():
        return False
    needle = window_class.lower()
    return needle in wm_class.lower() or needle in title.lower()


def find_game(window_class: str, display=None) -> Window | None:
    """The game window: the largest window is_game() accepts."""
    candidates = [w for w in list_windows(display)
                  if w.wm_class and is_game(w.wm_class, w.title, window_class)]
    # Largest wins: launcher and game can both be present, and the game is the
    # one filling a monitor.
    return max(candidates, key=lambda w: w.width * w.height, default=None)


def grab(window: Window, region: tuple[float, float, float, float] | None = None,
         display=None) -> Frame:
    """Capture the window, or a region of it given as fractions (x, y, w, h)."""
    own_display = display is None
    display = display or _open_display()
    try:
        if region is None:
            x, y, width, height = 0, 0, window.width, window.height
        else:
            fx, fy, fw, fh = region
            x = int(fx * window.width)
            y = int(fy * window.height)
            width = max(1, int(fw * window.width))
            height = max(1, int(fh * window.height))
            # Clamp: a region running past the edge makes XGetImage fail with
            # BadMatch, which surfaces as a crash rather than an exception.
            width = min(width, window.width - x)
            height = min(height, window.height - y)

        _last_error.clear()
        image = _x11.XGetImage(display, window.xid, x, y, width, height, ALL_PLANES, ZPIXMAP)
        _x11.XSync(display, 0)
        if not image or _last_error:
            raise CaptureError(
                f"XGetImage failed for window 0x{window.xid:x} "
                f"at {x},{y} {width}x{height} (window is {window.width}x{window.height})")

        contents = image.contents
        data = ctypes.string_at(contents.data, contents.bytes_per_line * contents.height)
        frame = Frame(contents.width, contents.height, contents.bytes_per_line,
                      contents.bits_per_pixel, data)
        _x11.XDestroyImage(image)
        return frame
    finally:
        if own_display:
            _x11.XCloseDisplay(display)


def to_image(frame: Frame):
    """Frame to a PIL image. Imported lazily so the module stays dependency-free."""
    from PIL import Image

    mode, raw_mode = ("RGBX", "BGRX") if frame.bits_per_pixel == 32 else ("RGB", "BGR")
    return Image.frombytes(mode, (frame.width, frame.height), frame.data,
                           "raw", raw_mode, frame.stride).convert("RGB")


def to_bgr(frame: Frame):
    """Frame to a BGR numpy array, the form OpenCV and the calibration dialog take.

    The dialog's "Capture the game window" handed a PIL image from `to_image`
    to code that read `.shape` and `.data`, which only an array has, so it
    raised on every press. One conversion here, used by every reader of a whole
    frame.
    """
    import numpy

    return numpy.ascontiguousarray(numpy.asarray(to_image(frame))[:, :, ::-1])
