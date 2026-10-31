"""Measure how much of a real inventory screen the pipeline reads.

It produces a number that can be compared after a change, and it fails when
the number drops below a floor (or rises above a ceiling) set just short of
today's measurement - see MIN_READ_RATE and the lines after it. Without this, a
regression from 15 names read to 1 is invisible until someone happens to look,
which is exactly what happened during development. The pitch table at the end
is only reported (see `check_pitch`).

Expected values in `expected.json` were read off the images by hand, not
produced by this code. A fixture generated from the implementation would only
confirm that the implementation still does what it did.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import testdb                                             # noqa: E402

from wft import config                                    # noqa: E402
from wft.data import db as dbq                            # noqa: E402
from wft.inventory import calibrate as cal, pagemodel as pm  # noqa: E402
from wft.inventory import scan as scan_mod                # noqa: E402
from wft.inventory.quantity import strip_rank              # noqa: E402
from wft.relic import match as match_mod                  # noqa: E402
from rapidfuzz import fuzz, process                       # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inventory"

# The limits below were set on 2026-10-29 from this test's own output over the 6
# frames in expected.json: 39 of 51 tracked tiles read (76%), 7 rows from 65
# untracked tiles (11%), 1 of those accepted without review (frame crop_a.png).
# Each limit sits just short of that, so one more lost tile or one more wrong
# row fails, and a real regression cannot hide. They are measured, not derived:
# when the reader or the fixtures change on purpose, measure again and move them.
#: Share of tracked tiles that must be read.
MIN_READ_RATE = 0.75
#: Share of untracked tiles (ore, gear) that may still produce a row.
MAX_ROW_RATE = 0.13
#: Rows accepted without review that are wrong - a wrong number in the database.
MAX_ACCEPTED_WRONG = 1


def read_frame(image, names: list[str], ignored: list[str],
               decoy_words: set[str]) -> dict[tuple[int, int], tuple[str, float]]:
    """Run the pipeline over a single still frame.

    The per-cell decision comes from `scan.decide_cell`, the same function the
    scan uses. This file used to carry its own copy, and the copy drifted: it
    was still matching case-sensitively long after that was fixed in the scan,
    and knew nothing about margins, decoys or fragments. Every change of the
    last few rounds was invisible to the number that was supposed to watch them.
    """
    words = [
        pm.Word(w["text"], float(w["left"]), float(w["top"]),
                float(w["width"]), float(w["height"]), 40.0, 0)
        for w in cal._ocr_words(image)
    ]
    grid = pm.build(words, [])
    if grid is None:
        return {}

    found: dict[tuple[int, int], tuple[str, float]] = {}
    for key, cell_words in pm.assign(words, grid).items():
        decision = scan_mod.decide_cell(
            pm.cell_hypotheses(cell_words, grid.row_pitch),
            names, ignored, decoy_words)
        if decision.name is not None:
            found[key] = (decision.name, decision.score, decision.lead)
    return found


def is_confident(score: float, lead: float) -> bool:
    """The scan's own confidence rule, so the test asks the same question."""
    return scan_mod.Candidate(
        column=0, row=0, name="", score=score, reading="", sightings=2,
        lead=lead).confident


def measured_pitch(image) -> float | None:
    """Row spacing as the picture shows it, independent of the OCR words.

    The grid model derives its pitch from where the item names sit. That is a
    reasonable way to find it and a useless way to check it — an error in the
    words is an error in the pitch, and the two agree by construction.

    This measures the drawn geometry instead. A row of pixels running through
    the gap between two tile rows is uniform; one running through artwork is
    not. Taking the standard deviation along each row and picking out the quiet
    ones gives the boundaries, and the spacing between distant boundaries
    divided by the rows between them gives the pitch.

    Worth having as a test rather than a script: a compressed virtual page went
    unnoticed for the project's whole life, because the crop offset had been
    calibrated on top of the compression and nothing compared the model against
    the image.
    """
    import numpy as np

    height, width = image.shape[:2]
    limit = int(width * 0.68)          # grid area; the detail pane is static
    grey = cv2.cvtColor(image[:, :limit], cv2.COLOR_BGR2GRAY).astype(np.float32)
    spread = grey.std(axis=1)
    spread = (spread - spread.mean()) / (spread.std() + 1e-9)

    # Fit a comb rather than hunting for individual minima. Picking out quiet
    # rows one by one needs a threshold, and a threshold that suits one screen
    # finds artwork on the next. A comb asks a different question - which
    # spacing puts every one of its teeth in a quiet row - and answers it from
    # the whole image at once.
    best_pitch, best_score = None, None
    for pitch in np.arange(340.0, 560.0, 0.5):
        for phase in np.arange(0.0, pitch, 3.0):
            rows = np.arange(phase, height, pitch).astype(int)
            rows = rows[rows < height]
            if len(rows) < 4:
                continue
            score = -float(spread[rows].mean())
            if best_score is None or score > best_score:
                best_pitch, best_score = float(pitch), score

    # No clean comb means no answer, which is better than a wrong one.
    if best_score is None or best_score < 0.30:
        return None
    return best_pitch


def check_pitch(names) -> int:
    """Compare the fitted pitch against the pitch the image shows."""
    print()
    print(f"{'frame':12s} {'fitted':>8s} {'measured':>9s}  {'off by':>7s}")
    print("-" * 46)
    worst = 0.0
    for filename in sorted(f.name for f in FIXTURES.glob("*.png")):
        image = cv2.imread(str(FIXTURES / filename))
        if image is None:
            continue
        words = [
            pm.Word(w["text"], float(w["left"]), float(w["top"]),
                    float(w["width"]), float(w["height"]), 40.0, 0)
            for w in cal._ocr_words(image)
        ]
        grid = pm.build(words, [])
        shown = measured_pitch(image)
        if grid is None or shown is None:
            print(f"{filename:12s} {'-':>8s} {'-':>9s}  {'-':>7s}")
            continue
        drift = abs(grid.row_pitch - shown) / shown
        worst = max(worst, drift)
        print(f"{filename:12s} {grid.row_pitch:8.1f} {shown:9.1f}  {drift:6.1%}")

    print("-" * 46)
    # Reported, not enforced. A still frame shows about four tile rows, and the
    # fit needs more than that - across these six frames it lands anywhere from
    # 369 to 599 while the image says 444 every time. That spread is a fact
    # about fitting to four rows, not a defect the scan suffers from: over a
    # recording the virtual page holds hundreds of rows and the fit comes out
    # within a third of a percent.
    #
    # The measurement column is the useful half. It is stable to a pixel across
    # frames and resolutions, which is what makes it worth having here at all -
    # `wf check-grid` runs the same comparison against a recording, where the
    # fitted number means something and a failure is worth acting on.
    print("fit is reported, not enforced: a still frame holds too few rows for "
          "it. Run `wf check-grid <recording>` for the check that bites.")
    return 0


def main() -> int:
    expected_path = FIXTURES / "expected.json"
    if not expected_path.is_file():
        # The screenshots and their expected values come from a real game and
        # stay local, so a fresh clone has this test and not its material.
        print(f"SKIPPED: no {expected_path.relative_to(FIXTURES.parent.parent.parent)}. "
              "It holds the tile names read off real inventory screenshots, which "
              "are not part of the repository; put your own screenshots and an "
              "expected.json in tests/fixtures/inventory/ to measure the reader.")
        return 0

    source = testdb.source_database(config)
    if not testdb.has_game_data(source):
        print(f"SKIPPED: the reader is measured against the game's item names. "
              f"{testdb.HOW_TO_GET_THE_DATA}")
        return 0

    expected = json.loads(expected_path.read_text())
    # A copy: `db.connect` keeps the schema current, which is a write.
    copy = Path(tempfile.mkdtemp()) / "warframe.sqlite"
    testdb.copy_database(source, copy)
    conn = dbq.connect(copy)
    names = list(scan_mod._tracked_names(conn))

    ignored = scan_mod._ignored_names(conn)
    decoy_words = {word for name in ignored for word in name.lower().split()
                   if len(word) >= 4}
    total_tiles = total_tracked = matched = 0
    total_untracked = total_spurious = total_spurious_confident = 0
    print(f"{'frame':12s} {'tiles':>6s} {'tracked':>8s} {'read':>6s}  {'rate':>5s} "
          f"{'other':>6s} {'rows':>5s} {'kept':>5s}")
    print("-" * 60)

    for filename, spec in sorted(expected.items()):
        image_path = FIXTURES / filename
        if not image_path.is_file():
            print(f"SKIPPED: frame {filename} is in expected.json and not in {FIXTURES}")
            continue
        image = cv2.imread(str(image_path))
        found = read_frame(image, names, ignored, decoy_words)

        tiles = spec["tiles"]
        # Only tiles whose name is something this tool tracks can be expected to
        # match. Ore, fish and gear are in the picture and deliberately ignored.
        #
        # The rank has to come off first. Expectations are recorded as the
        # screen writes them - "Elytron [26]", "Amesha [0]" - and a rank inside
        # the string drags the score under the bar, so every ranked tile counted
        # as untracked. That is why this said 20 tracked tiles out of 116: a
        # Warframe with a rank, which is most of them, was not being counted as
        # something the tool tracks.
        tracked = [t for t in tiles
                   if process.extractOne(strip_rank(t["name"])[0], names,
                                         scorer=fuzz.ratio,
                                         processor=str.casefold)[1] >= 92]
        hits = 0
        for tile in tracked:
            wanted, _ = strip_rank(tile["name"])
            for name, _score, _lead in found.values():
                if fuzz.ratio(match_mod.normalise(wanted), name) >= 88:
                    hits += 1
                    break

        # Tiles holding something untracked should produce nothing. Counting
        # only the hits measured half the job: the decoy pass exists to stop
        # ore, gear and cosmetics turning into rows, and nothing here saw
        # whether it did.
        untracked = len(tiles) - len(tracked)
        spurious = max(0, len(found) - hits)
        # A wrong row that lands in the review is a question; one that is
        # accepted is a wrong number in the database. Only the second is a bug.
        confident_rows = sum(1 for _n, s, l in found.values() if is_confident(s, l))
        spurious_confident = max(0, confident_rows - hits)

        total_tiles += len(tiles)
        total_tracked += len(tracked)
        total_untracked += untracked
        total_spurious += spurious
        total_spurious_confident += spurious_confident
        matched += hits
        rate = hits / len(tracked) if tracked else 0.0
        print(f"{filename:12s} {len(tiles):6d} {len(tracked):8d} {hits:6d}  "
              f"{rate:5.0%} {untracked:6d} {spurious:5d} {spurious_confident:5d}")

    print("-" * 60)
    overall = matched / total_tracked if total_tracked else 0.0
    print(f"{'total':12s} {total_tiles:6d} {total_tracked:8d} {matched:6d}  "
          f"{overall:5.0%} {total_untracked:6d} {total_spurious:5d} "
          f"{total_spurious_confident:5d}")
    noise = total_spurious / total_untracked if total_untracked else 0.0
    kept = total_spurious_confident / total_untracked if total_untracked else 0.0
    print(f"\n{total_untracked} tiles hold things this tool does not track. "
          f"{total_spurious} produced a row ({noise:.0%}), of which "
          f"{total_spurious_confident} were accepted without review ({kept:.0%}).")
    print("A row that goes to the review is a question; one accepted without "
          "review is a wrong number in the database.")
    check_pitch(names)

    failures = []
    if total_tracked == 0:
        failures.append("no frame was measured")
    else:
        if overall < MIN_READ_RATE:
            failures.append(f"read rate {overall:.1%} is under the floor "
                            f"{MIN_READ_RATE:.0%} ({matched} of {total_tracked})")
        if noise > MAX_ROW_RATE:
            failures.append(f"untracked tiles that produced a row: {noise:.1%} is over "
                            f"the ceiling {MAX_ROW_RATE:.0%} ({total_spurious} of "
                            f"{total_untracked})")
        if total_spurious_confident > MAX_ACCEPTED_WRONG:
            failures.append(f"{total_spurious_confident} wrong rows accepted without "
                            f"review, at most {MAX_ACCEPTED_WRONG} allowed")
    print()
    for failure in failures:
        print(f"FAIL: {failure}")
    if not failures:
        print(f"limits kept: read rate >= {MIN_READ_RATE:.0%}, rows from untracked "
              f"tiles <= {MAX_ROW_RATE:.0%}, wrong rows accepted <= {MAX_ACCEPTED_WRONG}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
