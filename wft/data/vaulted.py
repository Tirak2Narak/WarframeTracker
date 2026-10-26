"""Vault status, read off Digital Extremes' own drop tables.

The Public Export says what a relic contains but not whether it still drops.
DE publish that on one HTML page, the official drop tables: every mission,
bounty and enemy reward table in the game, followed by the contents of every
relic. A relic is available when some reward table on that page drops it, and
vaulted when none does. That is the definition the game itself works by, read
from the only party that knows it.

Until 2026-09-25 this came from WFCD's `Relics.json`, whose `vaulted` flag
looked like a stated fact. It is not one: WFCD's relic generator writes
`vaulted: drops.length === 0` over drop locations several builds behind DE, so
two days after Update 44 it called the five new Citrine relics vaulted and
fifteen rotated-out relics available. The inference from absence was never
avoided, only handed to someone slower.

The page is cut at its section headings (`<h3 id="...">`). The one section that
lists relic *contents* is excluded; every other section is a place things drop.
If the page does not look like that, nothing is written - a parse failure must
never turn into "everything is vaulted" or "everything drops" - and the page
that was refused is kept beside the good one, so the reason can be looked at.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import sqlite3
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wft.config import Config
from wft.data.publicexport import USER_AGENT, TIMEOUT

#: The section listing what each relic contains. Every other section is a
#: reward table, and a relic named in one of those drops somewhere.
CONTENTS_SECTION = "relicRewards"
#: Without these two the page is not the page this parser was written for.
REQUIRED_SECTIONS = ("missionRewards", CONTENTS_SECTION)

_HEADING = re.compile(r'<h3\s+id="([^"]+)"[^>]*>', re.I)
_CELL = re.compile(r"<(td|th)\b[^>]*>(.*?)</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
# "Axi A21 Relic", "Axi A21 Relic (Radiant)" - Elite Sanctuary Onslaught drops
# them refined - "Requiem Eterna Relic", "Requiem Iii Relic (Intact)". Exactly
# two words before "Relic", so an event mission called "Help Clem Retrieve The
# Relic" is not one. The tier word is not a fixed list: a relic of a tier this
# code has never heard of still counts, and is then looked up by name.
_RELIC = re.compile(
    r"^([A-Z][A-Za-z]*) ([A-Za-z0-9]+) Relic"
    r"(?: \((?:Intact|Exceptional|Flawless|Radiant)\))?$")
_LAST_UPDATE = re.compile(r"<b>\s*Last Update:\s*</b>\s*([^<]+)", re.I)


class DropTableError(RuntimeError):
    """The drop tables could not be fetched or read. Nothing was written."""


@dataclass
class DropTables:
    """What the page says, before anything is joined to the database."""

    #: Relic names some reward table drops, normalised (see `normalise`).
    available: set[str]
    #: Relic names the contents section has a table for, normalised.
    contents: set[str]
    #: Reward section id -> how many distinct relic names it drops.
    sections: dict[str, int]
    #: The page's own "Last Update" line, as printed ("23 September, 2026").
    page_date: str | None
    #: Normalised name -> the name as the page prints it, for messages.
    spelled: dict[str, str]


def normalise(name: str) -> str:
    """One key per relic, however it is spelled.

    The page title-cases Roman numerals ("Requiem Iii Relic") where the export
    writes "Requiem III Relic". Folding case is exact matching still - no
    distance, no threshold - it only stops capitals from being a difference.
    """
    return " ".join(name.split()).casefold()


def _cells(block: str):
    for tag, inner in _CELL.findall(block):
        yield tag.lower(), " ".join(html.unescape(_TAG.sub("", inner)).split())


def parse(text: str) -> DropTables:
    """Read which relics drop anywhere, or refuse.

    Raises DropTableError when the page is not recognisably the drop tables:
    a required section is missing, the contents section names no relic, or
    no reward table names one. Any of those means the markup changed, and a
    guess from a changed page is worse than yesterday's answer.
    """
    headings = [(match.start(), match.group(1)) for match in _HEADING.finditer(text)]
    ids = [section for _, section in headings]
    missing = [section for section in REQUIRED_SECTIONS if section not in ids]
    if missing:
        raise DropTableError(
            f"the page has no section {', '.join(missing)} - it is not the drop-table "
            f"page this reads (sections found: {', '.join(ids) or 'none'})")

    available: set[str] = set()
    contents: set[str] = set()
    sections: dict[str, int] = {}
    spelled: dict[str, str] = {}
    for index, (start, section) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        found: set[str] = set()
        for tag, cell in _cells(text[start:end]):
            match = _RELIC.match(cell)
            if not match:
                continue
            name = f"{match.group(1)} {match.group(2)} Relic"
            key = normalise(name)
            if section == CONTENTS_SECTION:
                # A contents table is headed by the relic, once per refinement;
                # its cells are the parts inside, never a relic.
                if tag == "th":
                    contents.add(key)
                    spelled.setdefault(key, name)
            elif tag == "td":
                # Reward tables head themselves with the node or bounty and
                # list what drops in cells. A heading that happens to end in
                # "Relic" names a mission, not a drop.
                found.add(key)
                spelled.setdefault(key, name)
        if section != CONTENTS_SECTION:
            sections[section] = len(found)
            available |= found

    if not contents:
        raise DropTableError(
            f"the {CONTENTS_SECTION} section lists no relic - its table layout has changed")
    if not available:
        raise DropTableError(
            "no reward table names a single relic - the table layout has changed "
            "or the page is incomplete")

    date = _LAST_UPDATE.search(text)
    page_date = " ".join(date.group(1).split()) if date else None
    return DropTables(available, contents, sections, page_date, spelled)


def apply_tables(connection: sqlite3.Connection, tables: DropTables,
                 notes: list[str], fetched_at: str | None = None) -> dict[str, int]:
    """Write vault status for every relic in the database. Does not commit.

    Joined on the relic's name, exactly: the page carries names only, and all
    four refinements of a relic share one. A relic the page drops is 0; every
    other relic is 1 - including relics the page never mentions at all, which
    is what "in no drop table" means.

    Refuses rather than writes when the join finds nothing, or would make
    every relic available: both mean the page and the database are talking
    about different things.
    """
    rows = connection.execute("SELECT unique_name, name FROM relics").fetchall()
    if not rows:
        raise DropTableError("the database holds no relics yet - run a full update first")
    known: dict[str, list[str]] = {}
    for unique, name in rows:
        known.setdefault(normalise(name), []).append(unique)

    matched = tables.available & known.keys()
    if not matched:
        raise DropTableError(
            f"none of the {len(tables.available)} relics the page drops is in the database "
            f"(e.g. {', '.join(sorted(tables.spelled[k] for k in tables.available)[:3])})")
    if len(matched) == len(known):
        raise DropTableError(
            f"every one of the {len(known)} relic names in the database would be "
            f"available - that is not a drop table, refusing to write it")

    unknown = tables.available - known.keys()
    if unknown:
        notes.append(
            f"{len(unknown)} relic(s) drop according to the page but are not in the "
            f"database yet - a full update loads them: "
            f"{', '.join(sorted(tables.spelled[k] for k in unknown))}")
    orphans = tables.available - tables.contents
    if orphans:
        notes.append(
            f"{len(orphans)} dropping relic(s) have no contents table on the page: "
            f"{', '.join(sorted(tables.spelled[k] for k in orphans))}")
    absent = known.keys() - tables.contents - tables.available
    if absent:
        notes.append(f"{len(absent)} relic name(s) in the database appear nowhere on "
                     f"the page and count as vaulted")
    if tables.page_date is None:
        notes.append("the page carries no 'Last Update' line; its date is unknown")

    connection.executemany(
        "UPDATE relics SET vaulted=? WHERE unique_name=?",
        [(0 if key in matched else 1, unique)
         for key, uniques in known.items() for unique in uniques])
    # Stamped so the interface can say how old this is. Vaulting rotates: a
    # relic that is vaulted today can be back in the drop tables next month,
    # and a ranking built on a stale answer is confidently wrong rather than
    # unsure. Nothing else in the database ages this way.
    connection.execute(
        "INSERT OR REPLACE INTO meta(key,value) "
        "VALUES('vaulted_updated_at', datetime('now'))")
    stamps = [("vaulted_source", "droptables")]
    if fetched_at:
        stamps.append(("droptables_fetched_at", fetched_at))
    if tables.page_date:
        stamps.append(("droptables_last_update", tables.page_date))
    connection.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", stamps)

    return {
        "page_relics_dropping": len(tables.available),
        "relic_names_known": len(known),
        "names_available": len(matched),
        "vaulted": connection.execute(
            "SELECT COUNT(*) FROM relics WHERE vaulted=1").fetchone()[0],
        "available": connection.execute(
            "SELECT COUNT(*) FROM relics WHERE vaulted=0").fetchone()[0],
    }


def _download(url: str, target: Path) -> str:
    """Fetch the page into `target`; return when, in UTC."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, \
            open(target, "wb") as handle:
        shutil.copyfileobj(response, handle)
        handle.flush()
        os.fsync(handle.fileno())
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def last_refresh(cfg: Config) -> str | None:
    """When vault status was last written, as stored, or None."""
    if not cfg.db_path.is_file():
        return None
    connection = sqlite3.connect(cfg.db_path)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key='vaulted_updated_at'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return row[0] if row else None


def _kept(cfg: Config) -> str:
    stamp = last_refresh(cfg)
    return ("vault status left as it was "
            + (f"(last refreshed {stamp} UTC)" if stamp else "(never refreshed)"))


def apply(cfg: Config, report: list[str] | None = None) -> dict[str, int]:
    """Download the drop tables, keep the page, write vault status.

    The page is downloaded next to where it will live and only moved into
    place once it has parsed and joined, so `droptables.html` is always a page
    that was accepted - other tools may read it. A refused page is kept as
    `droptables.rejected.html`. On any failure the database is not touched and
    the error says since when the vault status it still holds dates.
    """
    notes = report if report is not None else []
    url = str(cfg.get("sources.droptables_url", "") or "").strip()
    if not url:
        raise DropTableError(
            f"sources.droptables_url is not set in {cfg.source} - {_kept(cfg)}")
    if not cfg.db_path.is_file():
        # Connecting would quietly create an empty file; say what is missing.
        raise DropTableError(f"there is no database at {cfg.db_path} yet - run a full update")

    target = cfg.droptables_path
    partial = target.with_name(target.name + ".part")
    rejected = target.with_name(target.stem + ".rejected" + target.suffix)
    try:
        try:
            fetched_at = _download(url, partial)
        except (OSError, ValueError) as exc:
            raise DropTableError(f"download of {url} failed: {exc}") from exc
        tables = parse(partial.read_text(encoding="utf-8", errors="replace"))
        connection = sqlite3.connect(cfg.db_path)
        try:
            counts = apply_tables(connection, tables, notes, fetched_at=fetched_at)
            os.replace(partial, target)
            connection.commit()
        finally:
            connection.close()
    except (DropTableError, OSError, sqlite3.Error) as exc:
        if partial.exists():
            os.replace(partial, rejected)
            notes.append(f"the refused page is kept as {rejected}")
        raise DropTableError(f"{exc} - {_kept(cfg)}") from exc

    notes.append(f"drop tables kept at {target}"
                 + (f" (page dated {tables.page_date})" if tables.page_date else ""))
    return counts
