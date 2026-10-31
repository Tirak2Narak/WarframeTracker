"""The capture-OCR-match-score pipeline, run off the GUI thread.

Shared by the hotkey, the log watcher (not built yet: the log is a bonus
trigger, not the foundation) and the manual "test capture" button on the
Relic control page, so all three produce identical results.

Deliberately a plain function, not a bespoke QThread subclass: it is handed to
`ui.workers.task.TaskWorker`, which already provides the progress/finished/
failed signal plumbing every worker in this app uses.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from wft.relic.score import Verdict


@dataclass
class TriggerResult:
    verdict: Verdict | None
    error: str | None
    reward_count: int
    #: The relics the log says this reward screen was opened with, squad-wide.
    #: Carried through so the panel can take the player's own one off the shelf
    #: when a reward is picked - it cannot know which of the four was theirs
    #: without asking what is held.
    opened_relics: tuple[str, ...] = ()
    #: Where the whole reward screen was kept on disk (`wft.relic.kept_pictures`),
    #: or None when keeping is off or failed. The Rewards page names it from here
    #: instead of working the path out a second time.
    kept_picture: Path | None = None
    #: The player's own relic for this screen from the equip dialog in the log
    #: (`wft.relic.eelog.OwnRelic`), or None when the log does not say.
    own_relic: object = None
    #: Where the game window was on the desktop at the reading
    #: (`wft.capture.window.Placement`), or None when that could not be asked.
    #: The reward overlay lies exactly over it.
    placement: object = None
    #: The mask of the header's colour in the picture of the reading
    #: (`wft.relic.header.reference_mask`): what the overlay compares the live
    #: window with to tell that the reward screen is gone. None when the picture
    #: shows no header.
    header_mask: object = None
    #: When the reading was made (seconds, `time.time()`). The Rewards page tells
    #: "the same screen read again" from a new one by this and the rewards
    #: (`ui.views.relic_panel.same_screen`); tests set it.
    read_at: float = field(default_factory=time.time)


def run_trigger(cfg, report=lambda _msg: None) -> TriggerResult:
    """Capture the reward tiles, read them, and rank them.

    Candidates come from the relics the game logged for this reward screen,
    falling back to every prime part when the log says nothing.
    """
    from wft.capture import window as cap
    from wft.data import db as dbq
    from wft.ocr import engine as E
    from wft.relic import eelog, match as M, regions as R, score as S

    R.load_overrides(cfg)
    window_class = str(cfg.get("game.window_class", ""))
    game = cap.find_game(window_class) if window_class else None
    if game is None:
        return TriggerResult(None, f"Game window not found (class {window_class!r}).", 0)

    reward_count = int(cfg.get("game.reward_count", 4) or 4)
    reward_count = max(2, min(4, reward_count))
    report(f"window {game.width}x{game.height}, {reward_count} reward(s)")

    connection = dbq.connect(cfg.db_path)

    # The whole window, once, before anything is read: kept in memory as the
    # last reward screen (`wft.relic.last_screen`) and, when a folder is
    # configured, on disk. The regions below are still grabbed one by one
    # exactly as before - the reading path is unchanged.
    screen = _grab_the_screen(game, report)
    kept = _keep_the_screen(cfg, screen, report)
    where, header_mask = _overlay_facts(game, screen, report)

    # The game's own log names the four relics the squad opened, which turns
    # "which of 400 prime parts is this" into "which of 23". The reading does
    # not have to improve for the answer to; it only has to be closer to the
    # right name than to 22 others instead of 400.
    #
    # Anything the log does not answer falls back to every prime part, so a
    # missing log, a different install or a first run after launch costs
    # accuracy and nothing else.
    opened = eelog.read(str(cfg.get("paths.ee_log", "")))
    candidates = M.candidates_for_relics(connection, opened.unique_names)
    narrowed = bool(candidates)
    if candidates:
        names = ", ".join(
            row["name"] for row in connection.execute(
                "SELECT name FROM relics WHERE unique_name IN (%s)"
                % ",".join("?" * len(opened.unique_names)), opened.unique_names))
        report(f"relics in play: {names}  ({len(candidates)} candidates)")
    else:
        candidates = M.all_prime_parts(connection)
        report(f"no relics in the log — matching against "
               f"{len(candidates)} prime parts")

    from wft.relic import last_screen

    try:
        result = _read_and_rank(
            cfg, connection, candidates, reward_count, report,
            grab=lambda region: cap.to_image(cap.grab(game, region.as_tuple())),
            opened=opened.unique_names, narrowed=narrowed)
    except Exception as exc:  # noqa: BLE001 - kept, then raised as before
        # A reading that crashed is the RESCAN case too, and its picture is the
        # one worth calibrating on.
        if screen is not None:
            last_screen.remember(
                _as_bgr(screen), TriggerResult(None, str(exc), reward_count))
        raise
    if screen is not None:
        # With the reading, so a reader of the store sees the picture and what
        # came of it together. An error is a reading too: it is the RESCAN case.
        last_screen.remember(_as_bgr(screen), result)
    result.kept_picture = kept
    result.placement, result.header_mask = where, header_mask
    result.own_relic = eelog.own_relic(str(cfg.get("paths.ee_log", "")))
    return result


def _overlay_facts(game, screen, report):
    """(placement of the window, header mask of the picture) for the reward
    overlay; either is None when it cannot be had. Never raises: the
    overlay is not worth a reading."""
    from wft.capture import window as cap
    from wft.relic import header

    where = mask = None
    try:
        where = cap.placement(game)
    except Exception as exc:  # noqa: BLE001
        report(f"  could not place the game window: {exc}")
    try:
        if screen is not None:
            mask = header.reference_mask(_as_bgr(screen))
    except Exception as exc:  # noqa: BLE001
        report(f"  could not mask the header: {exc}")
    return where, mask


def _grab_the_screen(game, report):
    """The whole game window as a PIL image, or None when it cannot be had.

    A failure costs the picture and nothing else: this runs inside the fifteen
    seconds the screen is on, and the regions are grabbed on their own below.
    """
    from wft.capture import window as cap

    try:
        return cap.to_image(cap.grab(game))
    except Exception as exc:  # noqa: BLE001 - a picture is never worth the read
        report(f"  could not grab the whole screen: {exc}")
        return None


def _as_bgr(image):
    import numpy

    return numpy.ascontiguousarray(numpy.asarray(image)[:, :, ::-1])


def _keep_the_screen(cfg, image, report) -> Path | None:
    """Write the whole reward screen to disk, when the user asked for it.

    Off unless `capture.keep_rewards` is on and `capture.keep_rewards_dir`
    names a folder (`kept_pictures.configured_folder`); returns the path
    written, or None. Every failure is swallowed: this runs inside the fifteen
    seconds the screen is on, and a full disk must cost a picture, never a
    reading.

    It exists because the geometry has no net. The name band is placed by
    fractions a person calibrates by eye, and when it drifts by ten pixels the
    reading falls apart - "Lavos Prime Chassis Blueprint" came back as
    "| !W JU T Lavos P |me.CLaSSIs Blueprint" and was lost. Working out why
    took the one reward screen that happened to be among the screenshots
    on hand; a search of two weeks of captures found no second. Ten of these
    would make the band measurable, and the same corpus is what any argument
    about the score thresholds needs: today a wrong name on a bad crop and a
    right name on a good one are told apart by numbers measured on a handful
    of screens that no longer exist.
    """
    from wft.relic import kept_pictures

    folder = kept_pictures.configured_folder(cfg)
    if folder is None or image is None:
        return None
    try:
        path = kept_pictures.save(folder, image)
        report(f"  kept the screen at {path}")
        return path
    except Exception as exc:  # noqa: BLE001 - a picture is never worth the read
        report(f"  could not keep the screen: {exc}")
        return None


def run_on_image(cfg, frame, report=lambda _msg: None,
                 regions: dict | None = None, use_log: bool = True) -> TriggerResult:
    """The same reading and ranking, against a picture instead of the game.

    Calibrating the regions is only worth anything if the reading then works,
    and the only way to find that out used to be pressing the hotkey during a
    mission: fifteen seconds, once per run, no second try. A screenshot of a
    reward screen answers the same question at any time, and it is the same
    code path — testing anything else would be testing something the panel
    does not do.
    """
    from wft.data import db as dbq
    from wft.relic import eelog, match as M, regions as R

    R.load_overrides(cfg)
    # After the config, not before it: `load_overrides` replaces the module
    # constants wholesale, so values set by a caller beforehand were silently
    # thrown away — which made the calibration test measure the old regions
    # and report that the new ones did not work.
    if regions:
        for key, value in regions.items():
            setattr(R, key.upper(), value)

    reward_count = int(cfg.get("game.reward_count", 4) or 4)
    reward_count = max(2, min(4, reward_count))
    connection = dbq.connect(cfg.db_path)
    # The log names the relics of the *current* game state, which is the right
    # answer for a live capture and the wrong one for a picture taken earlier:
    # a screenshot from before the last mission would be matched against relics
    # it has nothing to do with, and read as the wrong parts. Calibration cares
    # whether the region holds legible text, so it asks for the full list.
    candidates = None
    if use_log:
        opened = eelog.read(str(cfg.get("paths.ee_log", "")))
        candidates = M.candidates_for_relics(connection, opened.unique_names)
    narrowed = bool(candidates)
    candidates = candidates or M.all_prime_parts(connection)

    height, width = frame.shape[:2]

    def crop(region):
        x, y, region_width, region_height = region.as_tuple()
        return frame[int(y * height):int((y + region_height) * height),
                     int(x * width):int((x + region_width) * width)]

    return _read_and_rank(cfg, connection, candidates, reward_count, report,
                          grab=crop, narrowed=narrowed)


#: Squad sizes to try, most common first. A four-player squad is the usual
#: case, so trying it first costs the others nothing.
REWARD_COUNTS = (4, 3, 2)


def _read_and_rank(cfg, connection, candidates, reward_count, report, grab,
                   opened=(), narrowed=False):
    """Read the reward row through `grab`, match the names, rank them.

    The row is centred, so three rewards do not sit where four would: reading a
    three-reward screen at the four-reward positions crops across the tile
    borders and returns wreckage — "NA'I.{ AKbror 'ime Stock & . Blu" for what
    the screen showed as two separate rewards.

    The count cannot come from configuration, because it changes per mission,
    and the log is no help either: its window can hold relics from the previous
    screen, so it said four for a three-reward run. So it is measured — read at
    each plausible count and keep whichever recognises the most. The usual case
    is four and is tried first, which costs the others nothing.
    """
    from wft.capture import window as cap
    from wft.ocr import engine as E
    from wft.relic import match as M, regions as R, score as S

    counts = ([reward_count] if reward_count not in REWARD_COUNTS
              else [n for n in REWARD_COUNTS
                    if n <= max(reward_count, max(REWARD_COUNTS))])

    # Every prime part there is, used as a ruler rather than as a candidate
    # list. Whether a crop landed on a tile is a question about the *text*, and
    # asking it of the narrow list gets the wrong answer: on a four-player
    # screen the log holds only this player's relic, so three of the four names
    # are real, legible and absent from that list. None when the log narrowed
    # nothing — the candidates already are every prime part, and matching each
    # tile against the same list twice would answer the same question twice.
    wide = M.all_prime_parts(connection) if narrowed else None

    attempts: list[tuple[int, int, list]] = []
    try:
        with E.Engine(psm=E.PSM_BLOCK) as engine:
            for attempt in counts:
                rewards, clean = _read_at(attempt, engine, grab, candidates,
                                          connection, report, wide)
                attempts.append((attempt, clean, rewards))
                # Every tile read as a name; nothing to gain by trying a layout
                # that fits fewer of them.
                if clean == attempt:
                    break
            rewards = pick_layout(attempts)
    except E.OcrUnavailable as exc:
        return TriggerResult(None, str(exc), reward_count)
    except cap.CaptureError as exc:
        return TriggerResult(None, str(exc), reward_count)
    finally:
        connection.close()

    # The order is read from the config on every screen, so a change in Settings
    # is in force for the next one.
    verdict = S.rank(rewards, S.order_from_config(cfg))
    report(f"  {verdict.headline}")
    return TriggerResult(verdict, None, len(rewards), tuple(opened))


def pick_layout(attempts: list[tuple[int, int, list]]) -> list:
    """Choose between the layouts that were read. `(slots, clean, rewards)`.

    Most tiles read as a name wins, and **a tie goes to the layout with fewer
    slots.** That second half is the whole function.

    A two-reward screen puts its two tiles either side of the centre, which is
    exactly where slots two and three of a four-tile layout fall. So the
    four-up read names both of them — and then reports two empty slots beside
    them as NOT RECOGNISED, and numbers the player's own card 2 instead of 1,
    which quietly breaks the rule that their card is always the leftmost.

    Both layouts named two. The old test kept the first of them, and the first
    is the widest, so the four-up read won every two-reward screen there has
    ever been. Naming everything you laid out beats naming the same number with
    empty slots left over.
    """
    if not attempts:
        return []
    return max(attempts, key=lambda a: (a[1], -a[0]))[2]


#: How much the second look blows a failed crop up. Measured on the Daikyu
#: screen, native 3840x2160: the name band is ~25 px of text, the bottom of
#: what the OCR model handles, and the raw crop read 'DaRvUPImeBaEnrnt' at 63
#: against a cutoff of 70. The same crop at 2x read 78, at 3x it read
#: 'Daikvu Prime Blueprint' at 95 — while every straddling crop of the wrong
#: layouts stayed under 63 at any size, so enlarging cannot promote wreckage
#: into a name.
ENLARGE = 3


def _enlarged(image):
    """The same crop, three times the size, for a second look.

    Handles both shapes `grab` produces - the live path hands over PIL
    images, the screenshot path numpy arrays. Anything that cannot be
    enlarged returns None and the first reading stands.
    """
    try:
        if hasattr(image, "save"):
            # A PIL image - told apart the way Engine._as_pil does, because
            # numpy arrays also answer to `resize`.
            from PIL import Image

            width, height = image.size
            return image.resize((width * ENLARGE, height * ENLARGE),
                                Image.BICUBIC)
        import cv2

        return cv2.resize(image, None, fx=ENLARGE, fy=ENLARGE,
                          interpolation=cv2.INTER_CUBIC)
    except Exception:  # noqa: BLE001 - a failed retry must not cost the read
        return None


def _read_at(reward_count, engine, grab, candidates, connection, report,
             wide):
    """One pass over the row, laid out for `reward_count` tiles.

    Returns the rewards and how many of the tiles read as a reward name at all
    — which is not the same as how many were identified, and the difference is
    what tells one layout from another.
    """
    from wft.ocr import engine as E
    from wft.data import db as dbq
    from wft.relic import match as M, regions as R, score as S

    # Once per screen, not per card.
    retained = dbq.retained_counts(connection)
    rewards = []
    clean = 0
    for position in range(1, reward_count + 1):
        image = grab(R.name_region(position - 1, reward_count))
        if image is None or getattr(image, "size", 1) == 0:
            report(f"  reward {position}: nothing to read there")
            rewards.append(S.Reward(position=position, raw_text=""))
            continue
        raw = E.read_lines(engine.read(image))
        found = M.match(raw, candidates)
        # Does this look like a reward name at all? Asked of every prime part
        # rather than of the relics in play, because a crop that straddles two
        # tiles is wreckage against any list, and a name from a squadmate's
        # relic is a name whatever this player opened. `wide` is None when the
        # candidates already are every prime part, and then `found` is that
        # answer.
        anything = M.match(raw, wide) if wide is not None else found

        # A tile neither list identified gets one more look, enlarged. The
        # name is usually there and legible - the Daikyu screen's first tile
        # was, to a person - but at native size the text sits at the bottom
        # of what the model resolves, and one bad pass turns a real reward
        # into NOT RECOGNISED for the whole fifteen seconds. Adopted only
        # when the second reading identifies something; wreckage stays
        # wreckage, so the layout scores keep their meaning.
        if not (found.part_unique or anything.part_unique):
            second = _enlarged(image)
            if second is not None:
                raw2 = E.read_lines(engine.read(second))
                found2 = M.match(raw2, candidates)
                anything2 = (M.match(raw2, wide) if wide is not None
                             else found2)
                if found2.part_unique or anything2.part_unique:
                    raw, found, anything = raw2, found2, anything2

        # The relics in play are an accuracy aid, not a guest list: on a
        # four-player screen the log holds only this player's relic, and
        # "Cedo Prime Barrel" was read perfectly and shown as NOT RECOGNISED
        # because it came out of somebody else's. Nor may a wrong narrow
        # answer block a right wide one — a squadmate's name can clear the
        # threshold against a similar part on this player's list while the
        # full list knows it at 100. So the wide answer wins whenever it is
        # good enough to be named at all *and* strictly better than the
        # narrow one — held to WIDE_NAME_SCORE, since six hundred candidates
        # forgive far less than twenty-three.
        if (anything.part_unique and anything.score >= M.WIDE_NAME_SCORE
                and (not found.part_unique or anything.score > found.score)):
            found = anything

        # Read at all, whichever list answered. The narrow list's rescue pass
        # identifies readings the wide ratio rejects, and an identified tile
        # that does not count is a layout that can lose the tie-break — which
        # throws the identified reward away.
        if found.part_unique or anything.part_unique:
            clean += 1
        elif S.looks_like_forma(raw):
            clean += 1

        report(f"  {reward_count}-up reward {position}: {raw!r} -> "
               f"{found.name!r} ({found.score:.0f})")
        if found.part_unique:
            rewards.append(
                S.load_reward(connection, found.part_unique, position,
                              raw, found.score, retained=retained))
        else:
            rewards.append(S.Reward(position=position, raw_text=raw))
    return rewards, clean
