"""Mastery from the public profile, for everything that has been sold.

Mastery is kept when an item is sold, and most players sell a Prime once it
hits rank 30. A scan of the inventory therefore cannot see the bulk of an
established account's mastery: there is nothing left on screen to read. Fable
called this the real hole in the feature, and it is.

Digital Extremes runs an endpoint the game itself uses for "view profile":

    https://api.warframe.com/cdn/getProfileViewingData.php?playerId=<id>

It answers without authentication and carries, under
`Results[0].LoadOutInventory.XPInfo`, one entry per item the account has ever
levelled - `{"ItemType": "/Lotus/Powersuits/Banshee/Banshee", "XP": 11953927}`
- whether or not the item is still owned. On the test profile 742 of 746
entries were for items no longer in the inventory.

Two things make it usable here. The `ItemType` is the same `unique_name` this
database is keyed on, so no name matching is involved: 744 of 746 entries
resolved directly. And affinity is monotonic - a Forma does not reset it, only
delays the profile page catching up - so "cumulative XP at or past the
threshold" is a sound test rather than a snapshot.

**The tool makes this request itself, once a day, and never more.** It is
exactly one GET (30 s timeout, no retry) when a valid account id is set and the
switch is on, plus one whenever the user presses the reload button. An earlier version
ruled the request out: fetching it from a program got the address rate-limited
within a dozen requests - 200, then 409, then 403 - and Warframe's own support
pages say a flagged address makes the *game* refuse to log in. It did: the
account could not sign in until the router had been restarted for a new
address. The request is back under the lessons of that: one request per day, a failure is
never retried (the next automatic one comes 24 hours later), every request is
logged, and the reload button warns after 5 requests in 24 hours. The saved
file stays as a second way in, for when the server refuses. See
`wft.profile.fetch` for the schedule and the request.
"""
from __future__ import annotations

import json
import sqlite3

#: The address the profile is fetched from - requested by `wft.profile.fetch`
#: (once a day at most) and shown for the user to open in a browser as the
#: second way in.
PROFILE_URL = "https://api.warframe.com/cdn/getProfileViewingData.php?playerId={}"

#: Cumulative affinity for rank 30, by class of thing. From the wiki's own
#: formula: 1000 x level^2 for a Warframe or companion, half that for a weapon.
#: Both of the numbers in common circulation are wrong - 450,000 is right for
#: weapons, but the widely quoted 1,600,000 for Warframes is not; it is 900,000.
RANK_30_FRAME = 900_000
RANK_30_WEAPON = 450_000

#: Rank 40 is not the same formula continued. Getting past 30 needs Forma, and
#: the maximum rank rises in steps, so the cumulative requirement is measured
#: rather than computed.
RANK_40_WEAPON = 3_710_000
RANK_40_MECH = 7_420_000

#: Which threshold a category takes.
#: "beast" is here on evidence from the repo's own profile, not from a rule:
#: all 15 beast entries in the saved profile sit at 904,258 XP or more and none
#: between 450,000 and 900,000, which fits the 900,000 cap of the other
#: companions. Not confirmed against the game's own display.
FRAME_CATEGORIES = {"warframe", "archwing", "companion", "necramech", "beast"}

#: Weapons that go to rank 40 rather than 30. Detected by name because there is
#: no field for it: the WFCD data set marks `maxLevelCap` on some of them and
#: leaves it off the Necramechs entirely. Treating one of these as mastered at
#: rank 30 would be wrong by 5 Forma.
RANK_40_PREFIXES = ("Kuva ", "Tenet ", "Coda ")
RANK_40_NAMES = {"Paracesis"}


def threshold(name: str, category: str) -> int:
    """Cumulative affinity at which this item counts as mastered."""
    if category == "necramech":
        return RANK_40_MECH
    if name in RANK_40_NAMES or name.startswith(RANK_40_PREFIXES):
        return RANK_40_WEAPON
    if category in FRAME_CATEGORIES:
        return RANK_30_FRAME
    return RANK_30_WEAPON


def profile_url(account_id: str) -> str:
    """The address to open in a browser, for a given account id."""
    return PROFILE_URL.format((account_id or "").strip().lower())


#: Mastery points per item at its rank cap, and the cumulative points a rank
#: costs. Used only to sanity-check a profile against its own stated rank —
#: nothing is decided by it, because quests, junctions and intrinsics also
#: award points and this cannot see them.
POINTS_FRAME = 6000
POINTS_WEAPON = 3000


def rank_points(rank: int) -> int:
    """Cumulative mastery points for a given rank."""
    return 2500 * rank * rank


def player_rank(data: dict) -> int | None:
    """The account's mastery rank, as the profile states it."""
    try:
        return int(data["Results"][0]["PlayerLevel"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def load_profile(path: str) -> dict:
    """The whole profile document, for the callers that want more than XPInfo."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise ValueError(f"Could not read that file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("That file is not the profile JSON.") from exc


def parse(raw: bytes, source: str = "That file") -> list[dict]:
    """The XP entries from the bytes of a profile document.

    One check for both ways in - a file saved from a browser and the answer of
    the request - so they cannot disagree about what a valid profile is. Every
    failure is turned into a plain message: this runs behind a button, and a
    KeyError means nothing to the person pressing it.
    """
    if not raw.strip():
        raise ValueError(
            f"{source} is empty. The profile address answers with nothing when "
            "given a player name instead of an account id — check the id.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{source} is not the profile JSON. Open the address in a browser "
            "and save the page it shows, not a screenshot of it.") from exc

    try:
        entries = data["Results"][0]["LoadOutInventory"]["XPInfo"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            f"{source} has no XPInfo section. It may be a different Warframe "
            "endpoint, or the profile may be private.") from exc
    if not isinstance(entries, list) or not entries:
        raise ValueError("The profile carries no levelling history.")
    return entries


def load(path: str) -> list[dict]:
    """The XP entries from a profile file saved out of a browser."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ValueError(f"Could not read that file: {exc}") from exc
    return parse(raw)


def resolve(connection: sqlite3.Connection, entries: list[dict]) -> dict:
    """Work out which of the account's items are mastered.

    Returns {"mastered": [(unique_name, name, xp)], "already": n, "unknown": n,
    "short": n} — what to set, what was already set, what this database has no
    row for, and what has been levelled but not to the cap.
    """
    from wft.data.db import NOT_FOR_MASTERY_CATEGORIES

    rows = {r["unique_name"]: r for r in connection.execute(
        "SELECT unique_name, name, category FROM items")}
    known = {r["item_unique"] for r in connection.execute(
        "SELECT item_unique FROM owned_items WHERE mastered = 1")}

    mastered, already, unknown, short = [], 0, 0, 0
    for entry in entries:
        unique = entry.get("ItemType")
        row = rows.get(unique)
        if row is None:
            unknown += 1
            continue
        if row["category"] in NOT_FOR_MASTERY_CATEGORIES:
            continue
        if int(entry.get("XP") or 0) < threshold(row["name"], row["category"]):
            short += 1
            continue
        if unique in known:
            already += 1
            continue
        mastered.append((unique, row["name"], int(entry["XP"])))

    mastered.sort(key=lambda item: item[1])
    return {"mastered": mastered, "already": already,
            "unknown": unknown, "short": short}
