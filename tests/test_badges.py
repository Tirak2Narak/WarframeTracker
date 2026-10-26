"""Stack counts read back from real tiles, against counts read off by eye.

The fixtures are tiles cut from real captures, named for what the
badge on them actually says. They exist because this reader was wrong half the
time and one of its answers was wrong *quietly*: a stack of 12 stored as 2,
which no later screen had any reason to doubt.

The same file holds the check of the second look at a tile's own name:
tiles cut wider than themselves carry the ends of their neighbours' names, which
turn the first word of a reading into a doubt.

Run: python3 tests/test_badges.py
"""
from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys

import cv2

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import testdb  # noqa: E402

from wft.inventory import quantity as quantity_mod  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "badges"

#: Badge crops at the resolution the scan reads them at, rather than tiles.
#:
#: The tiles above are the 320px copies the review keeps, and two real faults
#: did not survive that downscale: a blueprint's artwork read as "7" where
#: there was no badge at all, and a "2" read as "21" because a piece of the
#: Warframe behind it was tall enough to pass for a second figure. Both are
#: invisible in a shrunk tile - the marks that caused them merge away - so a
#: fixture cut from one cannot hold the scan to account.
CROPS = pathlib.Path(__file__).resolve().parent / "fixtures" / "badge-crops"

#: Tiles of a Prime Parts recording, named for the item they show. Before the
#: second look at a tile's own name, each was sent to the review with
#: "the reading does not clearly carry the word".
NAME_TILES = pathlib.Path(__file__).resolve().parent / "fixtures" / "name-tiles"


def read_one(image) -> int | None:
    """The badge on one tile, through the same steps the scan uses."""
    prepared = quantity_mod.badge_crop(image)
    if prepared is None:
        return None
    readings = []
    for glyph in quantity_mod.digit_crops(prepared):
        ok, buffer = cv2.imencode(".png", glyph)
        if not ok:
            return None
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "10",
             "-c", f"tessedit_char_whitelist={quantity_mod.DIGIT_ALLOWLIST}"],
            input=buffer.tobytes(), capture_output=True, timeout=30)
        readings.append(result.stdout.decode("utf-8", "replace").strip())
    if not readings:
        return None
    return quantity_mod.assemble(readings)


def read_prepared(prepared) -> int | None:
    """The same steps, starting from a badge crop instead of a tile."""
    readings = []
    for glyph in quantity_mod.digit_crops(prepared):
        ok, buffer = cv2.imencode(".png", glyph)
        if not ok:
            return None
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "10",
             "-c", f"tessedit_char_whitelist={quantity_mod.DIGIT_ALLOWLIST}"],
            input=buffer.tobytes(), capture_output=True, timeout=30)
        readings.append(result.stdout.decode("utf-8", "replace").strip())
    if not readings:
        return None
    return quantity_mod.assemble(readings)


def hairline_does_not_crash() -> bool:
    """A stroke beside the tick must be refused, not resized to nothing.

    Reached on a real capture: a tile carrying the infinity badge
    is drawn inside a box, and the box's right border - three pixels wide
    at three times the tick's height - passed every figure test and then
    went to `cv2.resize` at a scale that rounded its width to zero. That
    threw, and the scan with it. Synthetic, so the case is held even where
    the fixtures are not.
    """
    import numpy as np

    prepared = np.full((300, 600), 255, np.uint8)
    # A tick-shaped ring: square, hollow, about 0.4 filled like the real one.
    cv2.rectangle(prepared, (40, 40), (260, 260), 0, 26)
    # The stroke that crashed: three pixels wide, as tall as a figure, on
    # the tick's own line.
    prepared[52:248, 400:403] = 0
    try:
        crops = quantity_mod.digit_crops(prepared)
    except cv2.error as exc:
        print(f"FAIL  hairline beside the tick raised: {exc}")
        return False
    if crops:
        print(f"FAIL  hairline beside the tick read as {len(crops)} figure(s)")
        return False
    return True


def name_second_look() -> bool:
    """The own-name bands read the item without doubt where the wide ones do not.

    For every tile: the ordinary 36 bands, the best score taken as the re-read
    takes it, must still carry the doubt for at least some tiles (otherwise
    the second look has nothing to answer), and the same six crops narrowed to
    the tile's own name must name the same item with no doubt. The pieces are
    the ones `_attach_evidence` uses: `_name_band_image(own=True)` and
    `decide_cell`.
    """
    import sqlite3

    from wft import config
    from wft.inventory import scan as scan_mod
    from wft.ocr import engine as engine_mod

    files = sorted(NAME_TILES.glob("*.png"))
    if not files:
        # The tiles are the doubted ones of a Prime Parts scan.
        print(f"SKIPPED: no tiles in {NAME_TILES}. They are the doubted tiles "
              f"of a Prime Parts scan, cut from real captures, and are not part "
              f"of the repository.")
        return True
    source = testdb.source_database(config)
    if not testdb.has_game_data(source):
        print(f"SKIPPED: the second look matches tiles against the game's item "
              f"names. {testdb.HOW_TO_GET_THE_DATA}")
        return True
    # Read only: the names are all this needs, and `db.connect` may write
    # (it keeps the schema) - a test never writes the live database.
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    lookup = scan_mod._tracked_names(conn)
    names = list(lookup)
    if any(path.stem not in lookup for path in files):
        print("SKIPPED: the tracked names do not hold every fixture item. " + testdb.HOW_TO_GET_THE_DATA)
        return True
    ignored = scan_mod._ignored_names(conn)
    decoy_words = {word for name in ignored for word in name.lower().split()
                   if len(word) >= 4}

    def best_decision(tile, own):
        shared = scan_mod._TileShared(tile)
        best = None
        with engine_mod.Engine() as engine:
            for top in scan_mod.NAME_BAND_TOPS:
                for _label, *weights in scan_mod._SEPARATIONS:
                    prepared = scan_mod._name_band_image(
                        tile, weights, top, shared=shared, own=own)
                    if prepared is None:
                        continue
                    text = " ".join(engine.read(
                        prepared, psm=engine_mod.PSM_BLOCK).text.split())
                    if not text:
                        continue
                    decision = scan_mod.decide_cell(
                        [text], names, ignored, decoy_words, None)
                    # Only decisions for the item itself: the second look
                    # accepts nothing else.
                    if decision.name != path.stem:
                        continue
                    if own:
                        # Any doubt-free reading is taken, like the re-read.
                        if best is None or (decision.doubt is None
                                            and best.doubt is not None):
                            best = decision
                    elif best is None or decision.score > best.score:
                        # The ordinary re-read takes the best score.
                        best = decision
        return best

    wide_doubted = failed = 0
    doubted_names = []
    for path in files:
        tile = cv2.imread(str(path))
        if tile is None:
            print(f"FAIL  {path.name}: could not be read back")
            failed += 1
            continue
        own = best_decision(tile, True)
        if own is None or own.doubt is not None:
            print(f"FAIL  {path.name}: the own-name bands gave "
                  f"{None if own is None else own.reading!r} "
                  f"{'' if own is None else own.doubt}")
            failed += 1
        wide = best_decision(tile, False)
        if wide is not None and wide.doubt is not None:
            wide_doubted += 1
            doubted_names.append(path.stem)
    print(f"second look: {len(files) - failed} of {len(files)} tiles read "
          f"without doubt through their own name, {wide_doubted} of them "
          f"still doubted through the wide bands ({', '.join(doubted_names)})")
    if wide_doubted == 0:
        print("FAIL  no tile is doubted through the wide bands: the test no "
              "longer shows why the second look exists")
        return False
    return failed == 0


def main() -> int:
    if not hairline_does_not_crash():
        return 1
    if not name_second_look():
        return 1
    files = sorted(FIXTURES.glob("*.png"))
    if not files:
        # Not a failure. The fixtures are tiles cut from a real game,
        # and `tests/fixtures/.gitignore` keeps that content local for the
        # same reason the screenshots are kept local - so a fresh clone has
        # this test and not its material.
        print(f"SKIPPED: no tiles in {FIXTURES}. They are cut from real captures "
              f"and are not part of the repository; regenerate them by scanning "
              f"a folder of captures.")
        return 0

    right = wrong = missing = 0
    for path in files:
        expected_text = path.stem.rsplit("--", 1)[1]
        expected = None if expected_text == "none" else int(expected_text)
        image = cv2.imread(str(path))
        if image is None:
            print(f"FAIL  {path.name}: could not be read back")
            wrong += 1
            continue
        got = read_one(image)
        if got == expected:
            right += 1
        elif got is None:
            missing += 1
            print(f"miss  {path.name}: nothing read, expected {expected}")
        else:
            wrong += 1
            print(f"WRONG {path.name}: read {got}, expected {expected}")

    # And the crops, which hold the cases a shrunk tile cannot.
    crops = sorted(CROPS.glob("*.png")) if CROPS.is_dir() else []
    for path in crops:
        expected_text = path.stem.rsplit("--", 1)[1]
        expected = None if expected_text == "none" else int(expected_text)
        prepared = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if prepared is None:
            print(f"FAIL  {path.name}: could not be read back")
            wrong += 1
            continue
        got = read_prepared(prepared)
        if got == expected:
            right += 1
        elif got is None:
            missing += 1
            print(f"miss  {path.name}: nothing read, expected {expected}")
        else:
            wrong += 1
            print(f"WRONG {path.name}: read {got}, expected {expected}")

    print(f"{right} right, {wrong} wrong, {missing} missed, "
          f"of {len(files) + len(crops)}")
    # A wrong count is the failure that matters: it enters the inventory and
    # nothing downstream questions it. A missed one costs a correction.
    if wrong:
        print("FAILED — a wrong count is worse than no count")
        return 1
    if missing > len(files) // 5:
        print("FAILED — too many badges missed")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
