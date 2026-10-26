"""Reading which relics are in play from Warframe's own log.

The reward screen shows four rewards drawn from the four relics the squad
opened. Matching a reading against every part in the game means choosing from
724 names; matching it against the rewards of those four relics means choosing
from about 23. That is the difference between a likely answer and a certain one,
and it costs nothing but reading a file the game already writes.

The game logs its resource loader, which pulls in each relic's assets as the
player carrying it loads into the mission:

    4339.708 Sys [Info]: ResourceLoader 0x... (/Lotus/Types/Game/Projections/
                         T1VoidProjectionCalibanPrimeBBronze) Found 19 items...

That is well before the reward screen, not at it (58 to 248 s before it in 10
real screens), and a squad that fills up one by one spreads the four lines over
a minute or more. What ties them to a screen is the screen before: the
relics of a screen are the ones loaded since the previous one ended.

The path is exactly the `unique_name` in the relics table, so no name matching
is involved anywhere in this - the log and the database agree on the identifier.
The leading T1..T5 is the era (Lith, Meso, Neo, Axi, Requiem) and the trailing
Bronze/Silver/Gold/Platinum is the refinement; both are part of the identifier
and neither needs interpreting here.

This reads the log and nothing else. Warframe writes the account's e-mail
address and IP into the same file, so the only thing lifted out of it is the
projection path, by a pattern that cannot match either.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: Timestamp (seconds since game start) and projection path, from one line.
#: Anchored on the parenthesis the resource loader puts around the path so a
#: mention in some other context cannot be mistaken for a load.
_LINE = re.compile(
    r"^(\d+\.\d+)\s.*\((/Lotus/Types/Game/Projections/T\dVoidProjection\w+)\)")

#: How much of the end of the file to read per attempt. The log reaches several
#: megabytes in a session, and during a run the relic lines and the screen
#: before them are near the end - so the first block is usually enough and the
#: read stays cheap on the path that has half a second to spend.
#:
#: When it is not enough the search widens rather than giving up: a run from
#: earlier in the session sits further back (measured: 3.5MB from the end of a
#: 5.8MB log, which a single fixed window missed entirely). Reading the whole
#: file every time would pay that cost on every hotkey press instead.
TAIL_BYTES = 2_000_000

#: Stop widening here. Beyond this the log is from much earlier in the session
#: and "the most recent reward screen" is not a useful claim about it.
MAX_SEARCH_BYTES = 64_000_000

#: A reward screen has four rewards, one relic each.
MAX_RELICS = 4


@dataclass
class OpenedRelics:
    """What the log says about the most recent reward screen."""

    #: Relic unique_names, newest last. Empty when the log says nothing.
    unique_names: list[str]
    #: Seconds since game start of the newest sighting, for staleness checks.
    timestamp: float | None

    def __bool__(self) -> bool:
        return bool(self.unique_names)


def _blocks(path: str):
    """Yield successively larger tails of the file, ending at its end.

    Each yield covers everything the previous one did. That means a line seen
    in an earlier block is seen again, which costs a little scanning and buys
    not having to stitch results across block boundaries.
    """
    size = os.path.getsize(path)
    window = TAIL_BYTES
    while True:
        yield _tail(path, window)
        if window >= size or window >= MAX_SEARCH_BYTES:
            return
        window *= 4


def _tail(path: str, size: int) -> str:
    """The last `size` bytes of a file, decoded loosely.

    Decoding cannot be strict: the log is written by a Windows binary under
    Proton and carries whatever encoding the game felt like, and a decode error
    here would take out the whole relic path for the sake of a corrupted byte in
    an unrelated line.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        start = max(0, handle.tell() - size)
        handle.seek(start)
        data = handle.read()
    # A partial first line is dropped rather than parsed.
    if start:
        _, _, data = data.partition(b"\n")
    return data.decode("utf-8", "replace")


def read(log_path: str) -> OpenedRelics:
    """The relics of the most recent reward screen, or an empty result.

    Those are the relic lines since the screen before them: the last reward
    screen marker that precedes the newest relic line. The search widens until
    that marker is in the text, and takes the whole log when there is none - the
    first screen of a game session.

    It used to be a 45 s window back from the newest line, on the belief that the
    four load together as the screen opens. They load as each player joins: in one
    session the squad filled over 51 s and the player's own Axi A22 fell out,
    and the first screen of `tests/fixtures/eelog/session.txt` spread over 158 s
    and kept 1 of 4.

    Every failure returns empty rather than raising. This is an optimisation on
    a path that must not break: if the log is missing, unreadable, from a
    different install or simply has nothing in it, the caller matches against
    the full list exactly as it did before.
    """
    seen: list[tuple[float, str]] = []
    markers: list[float] = []
    try:
        for text in _blocks(os.path.expanduser(log_path)):
            seen, markers = [], []
            for line in text.splitlines():
                # Both the relic paths and the screen markers carry this word;
                # the cheap test keeps a whole-log read at about 30 ms for 12 MB.
                if "VoidProjection" not in line:
                    continue
                match = _LINE.match(line)
                if match:
                    seen.append((float(match.group(1)), match.group(2)))
                    continue
                match = _MARKER.match(line)
                if match:
                    markers.append(float(match.group(1)))
            if seen and any(at < seen[-1][0] for at in markers):
                break
    except OSError:
        return OpenedRelics([], None)

    if not seen:
        return OpenedRelics([], None)

    newest = seen[-1][0]
    before = [at for at in markers if at < newest]
    previous_screen = before[-1] if before else None
    # Newest first while de-duplicating: the loader mentions a relic several
    # times (queued, starting, completed). Should more than four distinct ones
    # follow the previous screen - not seen in any measured log - the four kept
    # are the most recent.
    recent: list[str] = []
    for timestamp, unique in reversed(seen):
        if previous_screen is not None and timestamp <= previous_screen:
            break
        if unique not in recent:
            recent.append(unique)
        if len(recent) >= MAX_RELICS:
            break

    recent.reverse()
    return OpenedRelics(recent, newest)


def reward_names(connection, unique_names: list[str]) -> list[str]:
    """Part names dropped by the given relics.

    Returns an empty list if the relics are unknown to the database, which sends
    the caller back to the full candidate list rather than to an empty one - a
    short list is only an improvement when it is the right short list.
    """
    if not unique_names:
        return []
    placeholders = ",".join("?" * len(unique_names))
    rows = connection.execute(
        f"SELECT DISTINCT p.name FROM relic_rewards r "
        f"JOIN parts p ON p.unique_name = r.part_unique "
        f"WHERE r.relic_unique IN ({placeholders})", unique_names).fetchall()
    return [row["name"] for row in rows]


# ---------------------------------------------------------------------------
# The player's own relic
# ---------------------------------------------------------------------------
#
# The four relics of a screen are everybody's; which one is the player's the
# loader lines do not say. Two other lines do, measured in the player's own log
# for six reward screens (English game language only):
#
#   * Equipping a relic in the lobby asks for confirmation, and the dialog is
#     logged with the relic's printed name:
#         Dialog::CreateOkCancel(description=Are you sure you want to equip
#         Meso D8 Relic for this mission? It will be consumed if ...
#     The last such dialog before the reward screen is the relic taken in. It is
#     logged again in each round of an endless run. A refined relic carries its
#     refinement after the name, "equip Lith G14 Relic [RADIANT] for this
#     mission?" ([EXCEPTIONAL] too; [FLAWLESS] not seen yet); an Intact one
#     carries none.
#   * Refining in the relic choice of a running mission asks first, naming the
#     refinement the relic starts from (none printed = Intact):
#         Dialog::CreateOkCancel(description=Are you sure you want to refine
#         and equip Lith S19 Relic [EXCEPTIONAL]? It will cost 100 Void Traces ...
#     and the equip dialog of the refined relic follows 1.5 to 2.6 s later (all
#     6 refines measured).
#   * The local player's loadout loader finishes with a line naming the player,
#     right after "Resource load completed (/Temp/LocalPlayer...)", and the
#     resource loader line that follows is the player's own relic with its
#     refinement (Bronze = Intact, Silver, Gold, Platinum). Other players load
#     as /Temp/OtherPlayerN_.... Not logged in the later rounds of an endless run.
#
# The dialog is the first source and the loader line alone the second,
# for a screen whose stretch of the log holds no equip dialog. In the 7 screens
# measured with both (5 in the test session, 2 more in a later one), the two
# named the same relic.
#
# The start of a reward screen is the first "VoidProjections" line about it
# (`OpenVoidProjectionRewardScreen...`, `<id> gets reward`); a screen lasts
# as long as those lines follow each other within `SCREEN_GAP_SECONDS`.
# Nothing here keeps a player name or an id: the lines are matched, not stored.

_EQUIP = re.compile(
    r"^(\d+\.\d+)\s.*Dialog::CreateOkCancel\(description=Are you sure you want "
    r"to equip (.+? Relic)(?: \[([A-Z]+)\])? for this mission\?")
_REFINE = re.compile(
    r"^(\d+\.\d+)\s.*Dialog::CreateOkCancel\(description=Are you sure you want "
    r"to refine and equip (.+? Relic)(?: \[([A-Z]+)\])?\? It will cost")
#: The refinement word of a dialog, to the identifier's suffix (db.REFINEMENTS).
_DIALOG_REFINEMENTS = {"INTACT": "Bronze", "EXCEPTIONAL": "Silver",
                       "FLAWLESS": "Gold", "RADIANT": "Platinum"}
_MARKER = re.compile(
    r"^(\d+\.\d+)\s.*VoidProjections: (?:OpenVoidProjectionRewardScreen|\w+ gets reward)")
_LOCAL_LOADED = re.compile(
    r"^(\d+\.\d+)\s.*Resource load completed .*\(/Temp/(LocalPlayer|OtherPlayer)")
_FINISHED = re.compile(r"^(\d+\.\d+)\s.*Game \[Info\]: \S+ loadout loader finished\.")
_PROJECTION = re.compile(
    r"^(\d+\.\d+)\s.*ResourceLoader .*\((/Lotus/Types/Game/Projections/"
    r"T\dVoidProjection\w+)\)")

#: Two markers further apart than this are two reward screens.
SCREEN_GAP_SECONDS = 30.0

#: Furthest back the search for the equip dialog goes. A long mission puts the
#: dialog far behind the reward screen; beyond this the answer is "unknown".
OWN_MAX_SEARCH_BYTES = 30_000_000

_REFINEMENT_SUFFIXES = ("Bronze", "Silver", "Gold", "Platinum")


@dataclass
class OwnRelic:
    """The player's own relic of the latest reward screen, from the log."""

    #: Printed name as the database has it ("Meso D8 Relic"), from the equip
    #: dialog; None when only the loader line named the relic.
    name: str | None
    #: "Bronze" | "Silver" | "Gold" | "Platinum" (the identifier's suffix), or
    #: None when the log does not say.
    refinement: str | None = None
    #: Seconds since game start of the line it came from.
    timestamp: float | None = None
    #: The identifier the local loader line named, when there was no dialog.
    unique: str | None = None
    #: Which line said so, for the panel log: "equip dialog" or "local loader".
    source: str = "equip dialog"
    #: The refinement the relic was refined from in this round's relic choice
    #: ("Bronze" ...), when a refine dialog of the same name came before the
    #: equip dialog; that one left the shelf.
    refined_from: str | None = None


def _own_from_text(text: str) -> OwnRelic | None:
    """The own relic of the last reward screen in `text`, or None.

    None also when the text holds no reward screen, or when the screen's
    stretch of it holds neither an equip dialog nor a local loader line - which
    includes a text that starts inside that stretch; the caller widens in that
    case.
    """
    markers: list[float] = []
    # Equip dialogs: (at, name, refinement suffix or None for an unprinted one).
    dialogs: list[tuple[float, str, str | None]] = []
    # Refine dialogs: (at, name, suffix refined from).
    refines: list[tuple[float, str, str | None]] = []
    # Local loader finishes followed by a projection line: (finished_at, unique).
    refinements: list[tuple[float, str]] = []
    last_kind = None
    armed: float | None = None
    for line in text.splitlines():
        if "Dialog::CreateOkCancel" in line:
            match = _EQUIP.match(line)
            if match:
                dialogs.append((float(match.group(1)), match.group(2),
                                _DIALOG_REFINEMENTS.get(match.group(3) or "")))
                continue
            match = _REFINE.match(line)
            if match:
                refines.append((float(match.group(1)), match.group(2),
                                _DIALOG_REFINEMENTS.get(match.group(3) or "INTACT")))
        elif "VoidProjections:" in line:
            match = _MARKER.match(line)
            if match:
                markers.append(float(match.group(1)))
        elif "Resource load completed" in line:
            match = _LOCAL_LOADED.match(line)
            if match:
                last_kind = match.group(2)
        elif "loadout loader finished" in line:
            match = _FINISHED.match(line)
            if match:
                armed = float(match.group(1)) if last_kind == "LocalPlayer" else None
                last_kind = None
        elif armed is not None and "Projections/T" in line:
            match = _PROJECTION.match(line)
            if match and float(match.group(1)) - armed <= 1.0:
                refinements.append((armed, match.group(2)))
            armed = None
    if not markers:
        return None

    # The last screen: walk back over markers that follow each other closely.
    end = len(markers) - 1
    start = end
    while start > 0 and markers[start] - markers[start - 1] <= SCREEN_GAP_SECONDS:
        start -= 1
    screen_start = markers[start]
    previous_end = markers[start - 1] if start > 0 else None

    inside = [dialog for dialog in dialogs
              if dialog[0] < screen_start and (previous_end is None or dialog[0] > previous_end)]
    if not inside:
        loaded = [(when, unique) for when, unique in refinements
                  if when < screen_start and (previous_end is None or when > previous_end)]
        if not loaded:
            return None
        when, unique = loaded[-1]
        return OwnRelic(name=None, refinement=_refinement_of(unique), timestamp=when,
                        unique=unique, source="local loader")
    at, name, printed = inside[-1]
    own = OwnRelic(name=name, timestamp=at)
    # The loader of this mission came after the dialog and before the screen.
    later = [unique for when, unique in refinements if at < when < screen_start]
    if later:
        own.refinement = _refinement_of(later[-1])
    if printed:
        own.refinement = printed
    # Refined in this round's relic choice: the refine dialog comes first.
    refined = [source for when, refined_name, source in refines
               if refined_name == name and when < at
               and (previous_end is None or when > previous_end)]
    if refined and own.refinement and refined[-1] != own.refinement:
        own.refined_from = refined[-1]
    return own


def _refinement_of(unique: str) -> str | None:
    """The refinement suffix of a relic identifier, or None."""
    return next((suffix for suffix in _REFINEMENT_SUFFIXES if unique.endswith(suffix)), None)


def own_relic(log_path: str) -> OwnRelic | None:
    """The player's own relic for the latest reward screen, or None.

    Reads from the end in growing blocks until the equip dialog of that screen,
    or its local loader line, is in the text, up to `OWN_MAX_SEARCH_BYTES`. Any failure is None: the
    caller then narrows by the leftmost card as before.
    """
    path = os.path.expanduser(log_path)
    try:
        size = os.path.getsize(path)
        window = TAIL_BYTES
        while True:
            found = _own_from_text(_tail(path, window))
            if found is not None:
                return found
            if window >= size or window >= OWN_MAX_SEARCH_BYTES:
                return None
            window = min(window * 4, OWN_MAX_SEARCH_BYTES)
    except OSError:
        return None


def own_relic_uniques(connection, own: OwnRelic, projections=()
                      ) -> tuple[list[str], str | None, list[str]]:
    """The identifiers an own relic can be: (narrowed, refinement, all of the name).

    A printed name is not unique (two "Lith G12 Relic" exist) and has four
    refinements. `all of the name` is every identifier with that name;
    `narrowed` is those at the refinement when it is known, else the same list.
    The refinement is the local loader's (`OwnRelic.refinement`); without it, if
    exactly one of the `projections` (identifiers the loader lines of this
    screen named) carries the name, that one's refinement - the same evidence
    from lines already read.

    An own relic from the loader line alone has no printed name but the exact
    identifier: `all of the name` is then that relic's four refinements only,
    not another relic printed with the same name.
    """
    name = own.name
    if name is None and own.unique:
        row = connection.execute(
            "SELECT name FROM relics WHERE unique_name = ?", (own.unique,)).fetchone()
        name = row["name"] if row else None
    rows = connection.execute(
        "SELECT unique_name FROM relics WHERE name = ? ORDER BY unique_name",
        (name,)).fetchall()
    every = [row["unique_name"] for row in rows]
    if own.unique and own.refinement:
        stem = own.unique[:-len(own.refinement)]
        every = [u for u in every if u[:-len(_refinement_of(u) or "")] == stem]
    refinement = own.refinement
    if refinement is None:
        named = [u for u in dict.fromkeys(projections) if u in every]
        if len(named) == 1:
            refinement = _refinement_of(named[0])
    narrowed = [u for u in every if u.endswith(refinement)] if refinement else every
    return narrowed, refinement, every
