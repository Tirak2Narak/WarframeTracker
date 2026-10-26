"""Running OCR over many frames, in two passes.

The obvious design is one pipeline: read a frame, encode it, hand it to a pool,
repeat. It was measurably wrong, and how it was wrong is worth keeping.

Measured on an idle machine, producing a frame costs 8-16ms and reading one
costs 160ms across fourteen workers — a ratio that says "the pool will always be
hungry, feed it from one thread". Under load the producer's tick was **240ms**,
not 8: the same sixteen cores were saturated by fourteen tesseracts, so decoding,
correlating and encoding each got a fraction of a core. One tesseract process was
alive at a time out of fourteen, and 9689 frames at 240ms is 39 minutes — which
is what the run actually took.

A producer thread with a queue does not fix it. The producer is CPU-bound in the
same saturated system whether or not OpenCV releases the GIL.

So the passes are separated in time rather than interleaved:

    pass 1   sample the video, write frames to disk — one process, all cores
    pass 2   OCR those files across a pool          — all cores

Idle measurements do not add up to a system whose stages compete for the same
cores. Two stages whose costs differ by a factor of three hundred belong apart.

Workers are plain subprocesses running the `tesseract` binary, so this needs no
extra dependency, and they read the files themselves — encoded frames never
cross a process boundary.
"""
from __future__ import annotations

import io
import multiprocessing as mp
import os
import subprocess
from dataclasses import dataclass

import cv2
import numpy as np

# Pillow assembles crop batches into multipage TIFFs (see read_crops). It is
# an optional dependency: without it every crop simply pays its own tesseract
# process again, which is correct and merely four times slower.
try:
    from PIL import Image as _pil_image
except ImportError:  # noqa: WPS440
    _pil_image = None

#: Fraction of the frame width that can hold tiles. The grid never spans the
#: whole screen; the remainder is the detail pane.
GRID_WIDTH_FRACTION = 0.68

#: JPEG quality for written frames. High enough that OCR reads the same text,
#: low enough to be cheap: encoding a 4K frame as PNG costs 102ms against 11ms
#: as JPEG. Checked rather than assumed — at 92, 95 and 98 the word differences
#: against PNG are noise in the artwork.
JPEG_QUALITY = 95

MIN_WORD_CONFIDENCE = 40.0

#: Crops per tesseract invocation in read_crops. Starting a tesseract process
#: and loading its language model costs ~98ms; recognising one name band costs
#: ~20ms — so one process per crop spends 82% of its time not reading. A dozen
#: crops per call amortises the start-up to ~8ms each while chunks stay small
#: enough that imap keeps every worker fed. Measured on the fixture crops:
#: 19.1ms/crop one-per-process, ~5ms/crop batched.
CROPS_PER_CALL = 12


@dataclass
class FrameRef:
    """One written frame, waiting to be read."""

    index: int
    scroll: float
    path: str


def default_workers() -> int:
    """One per core, minus two, so the machine stays usable while it runs."""
    return max(1, (os.cpu_count() or 4) - 2)


def encode(frame: np.ndarray) -> bytes:
    """Crop to the grid area and encode."""
    width = frame.shape[1]
    limit = int(width * GRID_WIDTH_FRACTION)
    cropped = frame[:, :limit] if limit < width else frame
    ok, buffer = cv2.imencode(".jpg", cropped, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buffer.tobytes() if ok else b""


def write_frame(frame: np.ndarray, directory: str, index: int) -> str:
    """Write one sampled frame for pass two to pick up."""
    path = os.path.join(directory, f"{index:06d}.jpg")
    with open(path, "wb") as handle:
        handle.write(encode(frame))
    return path


def _read_words(data: bytes) -> list[dict]:
    """One Tesseract run. At module level so it can be pickled."""
    if not data:
        return []

    # OMP_THREAD_LIMIT=1 is not a micro-optimisation, it is the whole point.
    # Tesseract is built with OpenMP and spreads a single page across every core
    # by default, so fourteen of them made each one four times slower than one
    # alone — the pool was fighting itself. One thread per process, and the
    # parallelism comes from the pool. Measured: 55.9s to 2.5s for 14 frames.
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "11", "tsv"],
        input=data, capture_output=True, timeout=180, env=environment)

    lines = result.stdout.decode("utf-8", "replace").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    try:
        columns = {n: header.index(n)
                   for n in ("left", "top", "width", "height", "conf", "text")}
    except ValueError:
        return []

    words = []
    for line in lines[1:]:
        fields = line.split("\t")
        # A row with the wrong field count is damaged: Tesseract's TSV is
        # unquoted and a recognised "word" can itself contain a tab.
        if len(fields) != len(header):
            continue
        text = fields[columns["text"]].strip()
        if not text:
            continue
        try:
            confidence = float(fields[columns["conf"]])
            box = {n: int(fields[columns[n]]) for n in ("left", "top", "width", "height")}
        except ValueError:
            continue
        if confidence >= MIN_WORD_CONFIDENCE:
            words.append({"text": text, **box})
    return words


def _init_worker() -> None:
    """Run once per pool worker, before any frame is touched.

    OpenCV's thread pool does not survive a fork intact, and these workers only
    shell out to tesseract anyway.
    """
    try:
        cv2.setNumThreads(0)
    except Exception:  # noqa: BLE001
        pass


def _run_ref(ref: FrameRef) -> tuple[int, float, list[dict]]:
    try:
        with open(ref.path, "rb") as handle:
            data = handle.read()
    except OSError:
        return ref.index, ref.scroll, []
    return ref.index, ref.scroll, _read_words(data)


def read_crops(crops: list[bytes], workers: int | None = None, progress=None,
               psm: int = 7, allowlist: str | None = None):
    """OCR a list of encoded images across the pool, yielding (index, text).

    The badge pass used to do this one at a time on the calling thread. Each
    call is a tesseract process start - about a second - and there are as many
    calls as there are tiles, so on a machine where the per-tile filter does not
    thin them out it took seven minutes for work the reading pass does in one.
    The pool was already here; the badge pass simply was not using it.

    The crops travel to the workers in batches of CROPS_PER_CALL, and each
    batch is one tesseract invocation over a multipage TIFF rather than one
    per crop — the process start dwarfs the recognition, and unlike the frames
    in read_files these jobs are uniform enough that batching does not idle
    the tail of the run. Results still come back one crop at a time.
    """
    workers = workers or default_workers()
    if not crops:
        return
    done = 0
    batch = CROPS_PER_CALL if _pil_image is not None else 1
    jobs = [(tuple(range(start, min(start + batch, len(crops)))),
             tuple(crops[start:start + batch]), psm, allowlist)
            for start in range(0, len(crops), batch)]
    context = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp
    with context.Pool(processes=workers, initializer=_init_worker) as pool:
        for pairs in pool.imap_unordered(_run_crop_batch, jobs, chunksize=1):
            for index, text in pairs:
                done += 1
                if progress:
                    progress(done)
                yield index, text


def read_tiles(tiles, prepare, workers: int | None = None, psm: int = 6):
    """Ship each tile once; the worker prepares and reads its own variants.

    The sweep used to build every band variant on the calling thread — six
    separations by six band tops per position, thresholded, enlarged and
    PNG-encoded — and push the encodings through the pool. On a 16-core
    machine that is ~56 seconds of one core preparing while fifteen wait,
    and ~1GB of encoded variants through the pipes. The tile crosses once
    instead, and the fan-out happens where the cores are.

    `prepare` must be a module-level function (a forked worker gets it by
    reference) taking one decoded tile and returning its prepared images in
    an order the caller relies on. Yields `(sequence, texts)` per tile with
    exactly one text per prepared image, "" where a page read as nothing —
    the caller rebuilds its per-variant bookkeeping from the counts.
    """
    workers = workers or default_workers()
    if not tiles:
        return
    jobs = [(sequence, tile, prepare, psm)
            for sequence, tile in enumerate(tiles)]
    context = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp
    with context.Pool(processes=workers, initializer=_init_worker) as pool:
        yield from pool.imap_unordered(_run_tile, jobs, chunksize=1)


def _run_tile(job) -> tuple[int, list[str]]:
    """One tile's whole fan-out, inside a worker. Module-level to pickle."""
    sequence, tile, prepare, psm = job
    try:
        variants = prepare(tile)
    except Exception:  # noqa: BLE001 - a tile that cannot fan out reads as absent
        return sequence, []
    return sequence, read_arrays(variants, psm)


def read_arrays(arrays, psm: int, allowlist: str | None = None) -> list[str]:
    """OCR prepared images in-process, one text per array, order kept.

    The worker-side half of `read_tiles`: the images already live here as
    numpy arrays, so they go into one multipage TIFF without ever being
    PNG-encoded. Same framing invariant and same fallback as
    `_run_crop_batch`; without Pillow each image pays its own process,
    which is correct and merely slower.
    """
    if not arrays:
        return []

    def singly() -> list[str]:
        out = []
        for array in arrays:
            ok, buffer = cv2.imencode(".png", array)
            out.append(_run_crop((0, buffer.tobytes() if ok else b"",
                                  psm, allowlist))[1])
        return out

    if _pil_image is None or len(arrays) < 2:
        return singly()
    try:
        images = [_pil_image.fromarray(array) for array in arrays]
        buffer = io.BytesIO()
        images[0].save(buffer, format="TIFF", save_all=True,
                       append_images=images[1:])
    except Exception:  # noqa: BLE001 - an array PIL cannot digest
        return singly()
    parts = _tiff_pages(buffer.getvalue(), len(images), psm, allowlist)
    return parts if parts is not None else singly()


def _tiff_pages(data: bytes, page_count: int, psm: int,
                allowlist: str | None) -> list[str] | None:
    """One tesseract call over a multipage TIFF, or None when it cannot be
    trusted — a failed process, or a page count the form feeds do not match.
    """
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    command = ["tesseract", "stdin", "stdout", "--psm", str(psm)]
    if allowlist:
        command += ["-c", f"tessedit_char_whitelist={allowlist}"]
    try:
        result = subprocess.run(
            command, input=data, capture_output=True,
            timeout=30 + 5 * page_count, env=environment)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.decode("utf-8", "replace").split("\f")
    if len(parts) != page_count:
        return None
    return [part.strip() for part in parts]


def _run_crop_batch(job) -> list[tuple[int, str]]:
    """One batch of prepared crops through one tesseract process.

    The batch is assembled into a multipage TIFF in memory and read in a
    single invocation, so the ~98ms process start and language-model load are
    paid once per CROPS_PER_CALL crops instead of once per crop.

    The one correctness hazard is page alignment: a result landing on the
    wrong neighbour would be far worse than a slow read. Tesseract separates
    pages with a form feed — `"A\\n\\fB\\n"` for two pages, `"\\f"` alone when
    the first of two is blank, no trailing separator — so splitting stdout on
    `\\f` yields EXACTLY one part per page, blank pages included (verified
    empirically against tesseract 5.5.3 for blank-first, blank-middle and
    blank-last orderings). That invariant is still checked on every batch:
    if the part count disagrees with the page count, or anything else about
    the batch goes wrong, the whole batch falls back to one-crop-per-call,
    which cannot misalign.
    """
    indices, datas, psm, allowlist = job
    live = [(position, data) for position, data in enumerate(datas) if data]
    if _pil_image is None or len(live) < 2:
        return _run_crops_singly(indices, datas, psm, allowlist)

    try:
        images = []
        for _, data in live:
            image = _pil_image.open(io.BytesIO(data))
            image.load()
            # TIFF carries each page's own photometric layout, but stay on
            # the three modes the probe covered rather than trusting every
            # exotic PNG variant to round-trip.
            if image.mode not in ("1", "L", "RGB"):
                image = image.convert("RGB")
            images.append(image)
        buffer = io.BytesIO()
        images[0].save(buffer, format="TIFF", save_all=True,
                       append_images=images[1:])
    except Exception:  # noqa: BLE001 - a crop PIL cannot digest
        return _run_crops_singly(indices, datas, psm, allowlist)

    parts = _tiff_pages(buffer.getvalue(), len(images), psm, allowlist)
    if parts is None:
        return _run_crops_singly(indices, datas, psm, allowlist)

    texts = [""] * len(indices)
    for (position, _), part in zip(live, parts):
        texts[position] = part
    return [(index, text) for index, text in zip(indices, texts)]


def _run_crops_singly(indices, datas, psm, allowlist) -> list[tuple[int, str]]:
    """The unbatched path: one tesseract per crop, immune to misalignment."""
    return [_run_crop((index, data, psm, allowlist))
            for index, data in zip(indices, datas)]


def _run_crop(job: tuple[int, bytes, int, str | None]) -> tuple[int, str]:
    """One prepared crop. At module level so it can be pickled.

    The page mode matters: a badge is one line and a name band is two or three,
    and reading a wrapped name as a single line loses most of it. Passing 7 for
    everything cost fifteen of twenty-one recoveries before it was noticed.
    """
    index, data, psm, allowlist = job
    if not data:
        return index, ""
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    command = ["tesseract", "stdin", "stdout", "--psm", str(psm)]
    if allowlist:
        # Only for a crop already known to hold what the list allows. Measured
        # on badge crops with the tick icon removed: 6 of 9 right without,
        # 8 of 9 right with, and the failures turned from letters read as
        # nothing into a digit read as nothing. Never for a crop whose content
        # is in question - forcing digits on artwork is how "111" gets born.
        command += ["-c", f"tessedit_char_whitelist={allowlist}"]
    try:
        result = subprocess.run(
            command, input=data, capture_output=True, timeout=30, env=environment)
    except (OSError, subprocess.SubprocessError):
        return index, ""
    return index, result.stdout.decode("utf-8", "replace").strip()


def read_files(refs: list[FrameRef], workers: int | None = None, progress=None,
               cancel=None):
    """Pass two: OCR the written frames, yielding results as they finish.

    Results arrive out of order, which is fine — every word carries the scroll
    offset of its own frame, so its place on the virtual page does not depend on
    the order it was read in.

    `cancel` is polled between results. This pass is the longer of the two, so
    a stop request that only reached the sampling pass was a button that did
    nothing for two thirds of the wait. Leaving the loop closes the pool through
    its context manager, and workers already mid-frame finish that frame — a
    second or so, not the rest of the queue.
    """
    workers = workers or default_workers()
    done = 0

    # "fork" explicitly. Python 3.14 defaults to forkserver, which re-imports
    # the main module in each worker: an interpreter startup per worker, and an
    # outright failure when the entry point is not an importable file. These
    # workers only shell out to tesseract, so there is nothing fork would leave
    # in a bad state — and with the passes separated there is no thread running
    # here to fork around, which is a second reason to keep them apart.
    context = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp
    with context.Pool(processes=workers, initializer=_init_worker) as pool:
        # chunksize stays 1: jobs are uneven in cost, and batching would idle
        # workers at the tail of the run. The multipage trick read_crops plays
        # is not worth it here either — a whole frame costs ~2.5s against the
        # ~98ms process start, so batching would buy four percent and pay for
        # it in tail latency.
        for index, scroll, words in pool.imap_unordered(_run_ref, refs, chunksize=1):
            done += 1
            if progress:
                progress(done)
            yield index, scroll, words
            if cancel is not None and cancel():
                pool.terminate()
                return
