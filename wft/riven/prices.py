"""What rivens for one weapon actually trade for.

A riven's price is dominated by its roll, and no feed can see the roll. What
this answers is the question before that one: whether rivens for a weapon are
worth anything at all, or whether the market treats them as dissolution
fodder. For that, Digital Extremes publish the best data there is — real
trades, not listings: every Monday 00:00 UTC they write one file per platform
with the previous week's riven-for-platinum trades, aggregated per weapon and
per rolled/unrolled. This module reads the PC file; the console variants
exist and are deliberately ignored.

**The format.** The file is not strict JSON. It is JavaScript object literals
— unquoted keys, single-quoted strings — and has been since 2019. It is
parsed tolerantly here: keys are quoted by a regex anchored on ``{`` and
``,`` (so a minified rewrite of the same notation still parses), then every
single quote becomes a double quote. That last step is the deliberate
breaking point: a weapon name containing an apostrophe would corrupt the
text and fail the JSON parse — loudly, as a RivenError, never as a wrong
number. No current name carries one; all 415 were checked.

**Why snapshots.** Only the current week is ever retrievable — Monday's write
replaces the file. So each week is stored in `riven_prices` and the history
grows locally; a week the tool never saw is a hole nothing can refill. That
is also why a short parse is refused outright (`SANITY_FLOOR`): a truncated
week once stored would count as done forever.

**On the numbers.** `pop` is the count of trades behind a row, and it is
small: 65 % of weapon rows carry pop ≤ 2 in a normal week. One trade is a
rumour, not a price, which is why the queries below carry a pooled pop
across every stored week — the honest measure of whether a weapon's market
exists at all.

**On asking too often.** The edge caches for a day (`max-age=86400`), so on
a Monday it may serve the previous file for hours. Retrying cannot hurry it,
and the data changes once a week — so this module attempts one fetch per
program run and no more, however often the page is opened.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from wft.data import db as dbq
#: Imported rather than copied. The string identifies this tool to the same
#: publisher; a second copy is a second truth waiting to drift.
from wft.world.fissures import USER_AGENT

TIMEOUT = 20

#: A normal week carries ~800 rows (801 counted in one week). A parse that
#: comes back smaller than this is format drift or a truncated answer, not a
#: quiet market — and a short week once stored would pass the staleness check
#: forever, leaving a silent permanent hole in the history. Refused instead.
SANITY_FLOOR = 500


class RivenError(RuntimeError):
    """The week's riven prices could not be read."""


# --- reading the file ----------------------------------------------------

#: An unquoted key, recognised only where object-literal syntax puts one:
#: right after `{` or `,`. Anchoring there is what keeps a weapon name that
#: happens to contain a colon from being mistaken for a key.
_KEY = re.compile(r"([{,]\s*)([A-Za-z_]\w*)\s*:")


def _refuse_constant(name: str):
    # json.loads accepts NaN and Infinity by default. A NaN stored as REAL
    # comes back as float('nan') and poisons every later sort comparison
    # silently; better to refuse the whole file out loud.
    raise ValueError(f"the file contains {name}")


def _decode(body: bytes) -> str:
    """The response body as text. utf-8-sig eats a BOM should one appear."""
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RivenError(f"the riven file is not readable text: {exc}") from exc


def _parse(text: str) -> list[dict]:
    """The file's rows, in this module's shape. Every failure is a RivenError.

    Values are read with `.get`: a missing field travels as NULL rather than
    costing the row, because the columns are nullable and a hole is honest.
    """
    quoted = _KEY.sub(r'\1"\2":', text).replace("'", '"')
    try:
        payload = json.loads(quoted, parse_constant=_refuse_constant)
    except ValueError as exc:
        raise RivenError(f"the riven file did not parse: {exc}") from exc
    rows = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "item_type": str(row.get("itemType") or ""),
            # compatibility is null on a category's veiled aggregate row;
            # stored as '' so the primary key can hold it.
            "weapon": str(row.get("compatibility") or ""),
            "rerolled": 1 if row.get("rerolled") else 0,
            "avg": row.get("avg"),
            "stddev": row.get("stddev"),
            "price_min": row.get("min"),
            "price_max": row.get("max"),
            "pop": row.get("pop"),
            "median": row.get("median"),
        })
    return rows


# --- which week this is --------------------------------------------------

def _monday_of(moment: datetime) -> str:
    """The Monday of this instant's UTC week, as an ISO date."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    day = moment.astimezone(timezone.utc).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def _current_week(now: datetime | None = None) -> str:
    """The publication Monday of the running week.

    Today, when today is a Monday — floored, never minus seven. And always
    from the UTC clock: on a machine east of UTC a local Monday just after
    midnight is still a UTC Sunday, a week whose file does not exist yet.
    """
    return _monday_of(now or datetime.now(timezone.utc))


def _week_from_last_modified(header: str | None) -> str | None:
    """The publication Monday the server itself names, or None.

    Floored onto its ISO week's Monday rather than taken as-is: a mid-week
    republish (a fix on a Wednesday) must not mint a Wednesday key that the
    staleness check would never match again.
    """
    if not header:
        return None
    try:
        moment = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    return _monday_of(moment)


# --- fetching and storing ------------------------------------------------

def _url(cfg) -> str:
    url = str(cfg.get("sources.riven_prices_url", "") or "").strip()
    if not url:
        raise RivenError("no sources.riven_prices_url is configured")
    return url


def fetch(cfg) -> tuple[str, list[dict]]:
    """One GET: (publication Monday, parsed rows).

    The week comes from the Last-Modified header, which names the write DE
    actually made. Missing header falls back to the current UTC week — the
    one poisonous residue case: an edge serving the old file *and* dropping
    the header would store old numbers under the new week. The header has
    never been observed missing; the fallback exists so a fetch that worked
    is not thrown away over a header.
    """
    request = urllib.request.Request(
        _url(cfg), headers={"User-Agent": USER_AGENT,
                            "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            last_modified = response.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        raise RivenError(f"{request.host} answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RivenError(f"could not reach {request.host}: {exc}") from exc
    week = _week_from_last_modified(last_modified) or _current_week()
    return week, _parse(_decode(body))


def store(conn: sqlite3.Connection, week: str, rows: list[dict]) -> None:
    """One week's rows as one transaction.

    `executemany` and a single commit, so a crash mid-write leaves no
    half-week behind to pass the staleness check. OR IGNORE rather than
    REPLACE: a week's data is immutable once published, and the first
    `fetched_at` is the honest record of when it was collected.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO riven_prices("
        " week, item_type, weapon, rerolled, avg, stddev,"
        " price_min, price_max, pop, median) "
        "VALUES(:week, :item_type, :weapon, :rerolled, :avg, :stddev,"
        " :price_min, :price_max, :pop, :median)",
        [{**row, "week": week} for row in rows])
    conn.commit()


#: One fetch attempt per program run, success or failure. On a Monday the
#: edge may serve the old week for hours (max-age=86400); the page refreshes
#: on every visit, and without this flag each visit would spend a request
#: asking a question whose answer changes once a week.
_attempted = False


def refresh_if_stale(cfg, db_path: Path) -> str | None:
    """Fetch and store the current week if it is not already stored.

    Returns the stored week, or None when nothing needed doing. Runs on a
    worker thread, which is why it opens (and closes) its own connections
    instead of borrowing a view's — sqlite objects stay on the thread that
    made them. The sanity floor sits before the write: a short parse is an
    error to show, never a week to record.
    """
    global _attempted
    expected = _current_week()
    probe = dbq.connect(db_path)
    try:
        have = probe.execute(
            "SELECT 1 FROM riven_prices WHERE week = ? LIMIT 1",
            (expected,)).fetchone()
    finally:
        probe.close()
    if have or _attempted:
        return None
    _attempted = True
    week, rows = fetch(cfg)
    if len(rows) < SANITY_FLOOR:
        raise RivenError(
            f"the riven file parsed to {len(rows)} rows where a week has "
            f"~800 — refusing to store it")
    conn = dbq.connect(db_path)
    try:
        store(conn, week, rows)
    finally:
        conn.close()
    return week


# --- what the page asks --------------------------------------------------
#
# These helpers are the only place the table is interpreted. The view reads
# them today; anything later that wants a riven price (a ranking, an export)
# calls the same functions or it will disagree with the page — that exact
# drift has bitten this codebase three times.

def weeks(conn: sqlite3.Connection) -> list[str]:
    """Stored weeks, newest first."""
    return [r["week"] for r in conn.execute(
        "SELECT DISTINCT week FROM riven_prices ORDER BY week DESC")]


def board(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per weapon: the newest week's numbers, plus the pooled pop.

    The pooled pop sums every stored week and both variants — the anti-noise
    measure. Two trades in each of six weeks is a thin but real market; two
    trades once ever is a rumour, and in the newest week alone the two look
    identical. A weapon absent from the newest week still gets its row, with
    the current columns NULL: it exists, it just did not trade this week.
    """
    return conn.execute(
        "WITH latest AS (SELECT MAX(week) AS week FROM riven_prices),"
        " pooled AS ("
        "  SELECT item_type, weapon, SUM(pop) AS pop_pooled"
        "  FROM riven_prices WHERE weapon != ''"
        "  GROUP BY item_type, weapon)"
        " SELECT p.item_type, p.weapon, p.pop_pooled,"
        "  u.median AS median_unrolled, u.pop AS pop_unrolled,"
        "  r.median AS median_rolled, r.avg AS avg_rolled,"
        "  r.pop AS pop_rolled, r.price_max AS max_rolled"
        " FROM pooled p"
        " LEFT JOIN riven_prices u ON u.item_type = p.item_type"
        "  AND u.weapon = p.weapon AND u.rerolled = 0"
        "  AND u.week = (SELECT week FROM latest)"
        " LEFT JOIN riven_prices r ON r.item_type = p.item_type"
        "  AND r.weapon = p.weapon AND r.rerolled = 1"
        "  AND r.week = (SELECT week FROM latest)"
        " ORDER BY p.weapon COLLATE NOCASE, p.item_type").fetchall()


def veiled(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The newest week's per-category aggregates for unopened rivens.

    These are the `compatibility: null` rows. They come rerolled=false only
    — an unopened riven cannot have been rolled — but that is the feed's
    promise, not a filter here, so a surprise variant would show rather
    than vanish.
    """
    return conn.execute(
        "SELECT item_type, rerolled, avg, stddev, price_min, price_max,"
        " pop, median FROM riven_prices"
        " WHERE weapon = '' AND week = (SELECT MAX(week) FROM riven_prices)"
        " ORDER BY item_type, rerolled").fetchall()


def history(conn: sqlite3.Connection, item_type: str,
            weapon: str) -> list[sqlite3.Row]:
    """Every stored week for one weapon, both variants, oldest first.

    Keyed by (item_type, weapon), never weapon alone: "Vinquibus" the rifle
    and "Vinquibus (Melee)" show why — one gun, two markets, and a bare name
    is one collision away from merging two weapons that merely share it.
    """
    return conn.execute(
        "SELECT week, rerolled, avg, stddev, price_min, price_max,"
        " pop, median FROM riven_prices"
        " WHERE item_type = ? AND weapon = ?"
        " ORDER BY week, rerolled", (item_type, weapon)).fetchall()
