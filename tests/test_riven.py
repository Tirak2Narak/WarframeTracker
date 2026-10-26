"""The riven price pipeline, against the file DE actually serves.

The fixture below is seven verbatim entries from weeklyRivensPC.json as
fetched on 2026-09-01 — unquoted keys, single quotes and all. It carries
every shape the parser must survive: a veiled aggregate row (compatibility
null), a weapon with both variants (Acceltra), an ampersand name (Ack &
Brunt) and the one name with a suffix in brackets (Vinquibus (Melee), which
is a different market from the rifle Vinquibus and must never be "tidied").

No network and no Qt: fetch is exercised through its parts (_decode, _parse,
week arithmetic) and refresh_if_stale through a stubbed fetch, because the
one thing a test must not do is spend a request on DE's edge.

Run: python3 tests/test_riven.py
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wft.riven import prices  # noqa: E402
from wft.riven.prices import RivenError  # noqa: E402

SCHEMA = (pathlib.Path(__file__).resolve().parent.parent
          / "wft" / "data" / "schema.sql")

FIXTURE = """[
  {
    itemType: 'Rifle Riven Mod',
    compatibility: null,
    rerolled: false,
    avg: 86.35,
    stddev: 489.68,
    min: 3,
    max: 5000,
    pop: 15,
    median: 10
  },
  {
    itemType: 'Rifle Riven Mod',
    compatibility: 'Acceltra',
    rerolled: false,
    avg: 49.88,
    stddev: 110.61,
    min: 8,
    max: 1000,
    pop: 10,
    median: 30
  },
  {
    itemType: 'Rifle Riven Mod',
    compatibility: 'Acceltra',
    rerolled: true,
    avg: 269.58,
    stddev: 601.07,
    min: 4,
    max: 5000,
    pop: 12,
    median: 80
  },
  {
    itemType: 'Melee Riven Mod',
    compatibility: 'Ack & Brunt',
    rerolled: false,
    avg: 35,
    stddev: 35.4,
    min: 10,
    max: 150,
    pop: 2,
    median: 25
  },
  {
    itemType: 'Melee Riven Mod',
    compatibility: 'Ack & Brunt',
    rerolled: true,
    avg: 270,
    stddev: 550.31,
    min: 5,
    max: 1500,
    pop: 1,
    median: 27.5
  },
  {
    itemType: 'Melee Riven Mod',
    compatibility: 'Vinquibus (Melee)',
    rerolled: true,
    avg: 295.5,
    stddev: 410.42,
    min: 12,
    max: 1000,
    pop: 1,
    median: 85
  },
  {
    itemType: 'Melee Riven Mod',
    compatibility: 'Vinquibus (Melee)',
    rerolled: false,
    avg: 102.35,
    stddev: 285.83,
    min: 10,
    max: 1500,
    pop: 4,
    median: 25
  }
]
"""


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    return conn


def _check_parse() -> list[str]:
    problems = []
    rows = prices._parse(FIXTURE)
    if len(rows) != 7:
        problems.append(f"parse: {len(rows)} rows from a 7-entry fixture")
        return problems
    veiled = rows[0]
    if veiled["weapon"] != "":
        problems.append(f"parse: null compatibility became {veiled['weapon']!r}")
    if veiled["item_type"] != "Rifle Riven Mod" or veiled["rerolled"] != 0:
        problems.append("parse: the veiled row lost its type or variant")
    names = {r["weapon"] for r in rows}
    for kept in ("Ack & Brunt", "Vinquibus (Melee)"):
        if kept not in names:
            problems.append(f"parse: {kept!r} did not survive verbatim")
    rolled = [r for r in rows if r["weapon"] == "Acceltra" and r["rerolled"]]
    if not rolled or rolled[0]["median"] != 80:
        problems.append("parse: Acceltra rolled median is not 80")
    sample = rows[1]
    if not isinstance(sample["price_min"], int) \
            or not isinstance(sample["avg"], float) \
            or not isinstance(sample["rerolled"], int):
        problems.append("parse: value types drifted")
    return problems


def _check_parse_minified() -> list[str]:
    import re

    minified = re.sub(r"\n\s*", "", FIXTURE)
    if prices._parse(minified) != prices._parse(FIXTURE):
        return ["minified: the same notation on one line parses differently"]
    return []


def _check_decode_bom() -> list[str]:
    body = b"\xef\xbb\xbf" + FIXTURE.encode("utf-8")
    try:
        rows = prices._parse(prices._decode(body))
    except RivenError as exc:
        return [f"BOM: a byte-order mark broke the parse: {exc}"]
    if len(rows) != 7:
        return ["BOM: rows went missing behind a byte-order mark"]
    return []


def _check_nan_is_refused() -> list[str]:
    poisoned = FIXTURE.replace("stddev: 489.68", "stddev: NaN", 1)
    try:
        prices._parse(poisoned)
    except RivenError:
        return []
    return ["NaN: a NaN slipped through and will poison every later sort"]


def _check_week_arithmetic() -> list[str]:
    problems = []
    cases = [
        ("Mon, 31 Aug 2026 00:00:03 GMT", "2026-08-31",
         "a Monday header names its own day"),
        ("Wed, 02 Sep 2026 12:00:00 GMT", "2026-08-31",
         "a mid-week republish floors onto its Monday"),
    ]
    for header, expected, why in cases:
        got = prices._week_from_last_modified(header)
        if got != expected:
            problems.append(f"week: {why} — wanted {expected}, got {got}")
    if prices._week_from_last_modified(None) is not None:
        problems.append("week: a missing header should answer None")
    if prices._week_from_last_modified("not a date") is not None:
        problems.append("week: an unparseable header should answer None")
    monday_early = datetime(2026, 8, 31, 0, 30, tzinfo=timezone.utc)
    if prices._current_week(monday_early) != "2026-08-31":
        problems.append("week: Monday 00:30 UTC is already the new week")
    sunday_late = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    if prices._current_week(sunday_late) != "2026-08-24":
        problems.append("week: Sunday 23:59 UTC still belongs to the old week")
    return problems


def _check_store_idempotent() -> list[str]:
    problems = []
    conn = _memory_db()
    rows = prices._parse(FIXTURE)
    prices.store(conn, "2026-08-31", rows)
    count = conn.execute("SELECT COUNT(*) FROM riven_prices").fetchone()[0]
    if count != 7:
        problems.append(f"store: wrote {count} of 7 rows")
    # Pin the provenance, then store the same week again: OR IGNORE must
    # keep both the rows and the first fetched_at.
    conn.execute("UPDATE riven_prices SET fetched_at = '2000-01-01 00:00:00'")
    conn.commit()
    prices.store(conn, "2026-08-31", rows)
    again = conn.execute("SELECT COUNT(*) FROM riven_prices").fetchone()[0]
    if again != count:
        problems.append(f"store: a second store changed the count to {again}")
    kept = conn.execute(
        "SELECT COUNT(*) FROM riven_prices "
        "WHERE fetched_at = '2000-01-01 00:00:00'").fetchone()[0]
    if kept != count:
        problems.append("store: a second store rewrote fetched_at")
    conn.close()
    return problems


class _Cfg:
    """The one config shape these functions read: a dotted get."""

    def __init__(self, **values):
        self.values = values

    def get(self, dotted, default=None):
        return self.values.get(dotted, default)


def _check_unconfigured_url() -> list[str]:
    try:
        prices._url(_Cfg())
    except RivenError as exc:
        if "riven_prices_url" in str(exc):
            return []
        return [f"config: the error does not name the missing key: {exc}"]
    return ["config: a missing URL should be a RivenError, not a request"]


def _check_refresh_guards() -> list[str]:
    """The sanity floor refuses a short week; the flag limits attempts."""
    problems = []
    real_fetch, real_flag = prices.fetch, prices._attempted
    with tempfile.TemporaryDirectory() as scratch:
        db_path = pathlib.Path(scratch) / "warframe.sqlite"
        week = prices._current_week()
        try:
            # A short parse must raise and store nothing.
            prices._attempted = False
            prices.fetch = lambda cfg: (week, prices._parse(FIXTURE))
            try:
                prices.refresh_if_stale(_Cfg(), db_path)
                problems.append("floor: 7 rows were accepted as a full week")
            except RivenError:
                pass
            conn = sqlite3.connect(db_path)
            stored = conn.execute(
                "SELECT COUNT(*) FROM riven_prices").fetchone()[0]
            conn.close()
            if stored:
                problems.append(f"floor: {stored} rows were stored anyway")

            # One attempt per run: the failure above already spent it.
            def refuse(cfg):
                raise AssertionError("a second fetch was attempted")

            prices.fetch = refuse
            if prices.refresh_if_stale(_Cfg(), db_path) is not None:
                problems.append("flag: a spent attempt still claimed a week")

            # A full week stores and reports itself, once.
            base = prices._parse(FIXTURE)
            full = [dict(row, weapon=f"Weapon {n}") for n in range(600)
                    for row in base[1:2]]
            prices._attempted = False
            prices.fetch = lambda cfg: (week, full)
            got = prices.refresh_if_stale(_Cfg(), db_path)
            if got != week:
                problems.append(f"refresh: stored {got}, expected {week}")
            prices.fetch = refuse
            if prices.refresh_if_stale(_Cfg(), db_path) is not None:
                problems.append("refresh: a stored week was fetched again")
        finally:
            prices.fetch, prices._attempted = real_fetch, real_flag
    return problems


def _seeded() -> sqlite3.Connection:
    """Two stored weeks: the fixture week and a thinner one before it."""
    conn = _memory_db()
    prices.store(conn, "2026-08-31", prices._parse(FIXTURE))
    earlier = [
        {"item_type": "Rifle Riven Mod", "weapon": "Acceltra", "rerolled": 0,
         "avg": 40.0, "stddev": 90.0, "price_min": 5, "price_max": 800,
         "pop": 3, "median": 25},
        {"item_type": "Rifle Riven Mod", "weapon": "Acceltra", "rerolled": 1,
         "avg": 200.0, "stddev": 400.0, "price_min": 10, "price_max": 3000,
         "pop": 5, "median": 70},
        {"item_type": "Pistol Riven Mod", "weapon": "Acrid", "rerolled": 0,
         "avg": 120.0, "stddev": 140.0, "price_min": 30, "price_max": 500,
         "pop": 1, "median": 74.5},
    ]
    prices.store(conn, "2026-08-24", earlier)
    return conn


def _check_queries() -> list[str]:
    problems = []
    conn = _seeded()
    if prices.weeks(conn) != ["2026-08-31", "2026-08-24"]:
        problems.append(f"weeks: wrong order or content: {prices.weeks(conn)}")

    rows = {(r["item_type"], r["weapon"]): r for r in prices.board(conn)}
    acceltra = rows.get(("Rifle Riven Mod", "Acceltra"))
    if acceltra is None:
        problems.append("board: Acceltra is missing")
    else:
        if acceltra["median_unrolled"] != 30 or acceltra["median_rolled"] != 80:
            problems.append("board: not the newest week's medians")
        if acceltra["pop_pooled"] != 10 + 12 + 3 + 5:
            problems.append(
                f"board: pooled pop is {acceltra['pop_pooled']}, not 30")
        if acceltra["max_rolled"] != 5000:
            problems.append("board: rolled max did not come along")
    acrid = rows.get(("Pistol Riven Mod", "Acrid"))
    if acrid is None:
        problems.append("board: a weapon absent this week lost its row")
    elif acrid["median_unrolled"] is not None:
        problems.append("board: an old week's numbers posed as current")
    elif acrid["pop_pooled"] != 1:
        problems.append("board: pooled pop ignores older weeks")
    if ("Rifle Riven Mod", "") in rows:
        problems.append("board: the veiled aggregate leaked onto the board")

    hidden = prices.veiled(conn)
    if len(hidden) != 1 or hidden[0]["item_type"] != "Rifle Riven Mod" \
            or hidden[0]["rerolled"] != 0:
        problems.append("veiled: expected exactly the one unrolled Rifle row")

    story = prices.history(conn, "Rifle Riven Mod", "Acceltra")
    if [r["week"] for r in story] != ["2026-08-24", "2026-08-24",
                                      "2026-08-31", "2026-08-31"]:
        problems.append("history: not every week, oldest first")
    if prices.history(conn, "Melee Riven Mod", "Vinquibus (Melee)") == []:
        problems.append("history: the bracketed name found nothing")
    conn.close()
    return problems


def main() -> int:
    checks = [
        ("the fixture parses", _check_parse),
        ("minified input parses the same", _check_parse_minified),
        ("a BOM is harmless", _check_decode_bom),
        ("NaN is refused", _check_nan_is_refused),
        ("week arithmetic", _check_week_arithmetic),
        ("storing is idempotent", _check_store_idempotent),
        ("a missing URL is named", _check_unconfigured_url),
        ("refresh guards", _check_refresh_guards),
        ("board, veiled and history", _check_queries),
    ]
    problems = []
    for label, check in checks:
        try:
            problems.extend(check())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}: blew up: {exc!r}")

    for problem in problems:
        print("   ", problem)
    print(f"riven prices       : {len(checks) - sum(1 for _ in problems)} "
          f"of {len(checks)} checks clean"
          if not problems else
          f"riven prices       : {len(problems)} problem(s) "
          f"across {len(checks)} checks")
    if problems:
        print(f"FAILED — {len(problems)} problem(s)")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
