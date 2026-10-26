"""Fetching the profile once a day, and what is done with the answer.

The request is exactly one GET on `mastery.profile_url(id)`: the tool's
user agent, a timeout of `TIMEOUT` seconds, no second attempt. An earlier version kept the tool
from making it at all, because about a dozen of them in some minutes got the
address flagged and the game refused to log in. The rules that came out of
that hold here:

- at most 1 automatic request per `INTERVAL`, counted from the newest request
  of any kind (automatic or the reload button) - a failed one included, so a
  409 is not answered with another try but with a day's wait;
- every request is a row in `fetch_log`, which is also where the schedule is
  read from, so it survives a restart;
- the reload button is not locked, but the page warns from `WARN_COUNT`
  requests inside `WARN_WINDOW`.

Nothing here touches Qt or reads a clock by itself: times come in as
arguments, so the schedule can be driven with a made-up clock. The network
half (`fetch_profile`) runs on a worker thread and touches no database; the
database half (`finish`) runs on the GUI thread with a connection of its own.
"""
from __future__ import annotations

import email.utils
import http.client
import os
import re
import sqlite3
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wft.data import db as dbq
from wft.data.publicexport import USER_AGENT
from wft.profile import mastery

JOB = "profile"
#: One automatic request per this long, counted from the newest request.
INTERVAL = timedelta(hours=24)
TIMEOUT = 30
#: The reload button warns when this many requests lie inside the window.
WARN_COUNT = 5
WARN_WINDOW = timedelta(hours=24)
#: The newest answer, kept as one file in the data folder and replaced each time.
PROFILE_FILE = "profile.json"
#: UTC time of the copy the file holds, in `meta`.
SNAPSHOT_KEY = "profile_snapshot_at"

_STAMP = "%Y-%m-%d %H:%M:%S"
_ID = re.compile(r"[0-9a-f]{24}", re.IGNORECASE)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    """UTC text of a time, the form `fetch_log.at` and the snapshot use."""
    return moment.astimezone(timezone.utc).strftime(_STAMP)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, _STAMP).replace(tzinfo=timezone.utc)


def valid_id(account_id) -> bool:
    """24 hex characters, nothing else."""
    return bool(_ID.fullmatch(str(account_id or "").strip()))


def enabled(cfg) -> bool:
    """A valid account id is set and the switch is on."""
    return (valid_id(cfg.get("profile.account_id", ""))
            and bool(cfg.get("profile.auto_fetch", True)))


def profile_path(cfg) -> Path:
    return cfg.data_dir / PROFILE_FILE


# --- the log and the schedule ------------------------------------------------

def record(connection: sqlite3.Connection, at: datetime, kind: str,
           status: int | None, detail: str, job: str = JOB) -> int:
    """Log one request; returns its row id, for `settle` to fill in later."""
    cursor = connection.execute(
        "INSERT INTO fetch_log(job, at, kind, status, detail) VALUES(?,?,?,?,?)",
        (job, stamp(at), kind, status, detail))
    connection.commit()
    return int(cursor.lastrowid)


def settle(connection: sqlite3.Connection, row_id: int, status: int | None,
           detail: str) -> None:
    """Fill in how a request logged at its start ended."""
    connection.execute(
        "UPDATE fetch_log SET status = ?, detail = ? WHERE id = ?",
        (status, detail, row_id))
    connection.commit()


def last(connection: sqlite3.Connection, job: str = JOB):
    """The newest request of this job, or None."""
    return connection.execute(
        "SELECT * FROM fetch_log WHERE job = ? ORDER BY at DESC, id DESC LIMIT 1",
        (job,)).fetchone()


def count_recent(connection: sqlite3.Connection, now: datetime,
                 job: str = JOB, window: timedelta = WARN_WINDOW) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE job = ? AND at > ?",
        (job, stamp(now - window))).fetchone()[0])


def next_due(connection: sqlite3.Connection, job: str = JOB) -> datetime | None:
    """When the next automatic request may come; None when there was never one."""
    row = last(connection, job)
    return parse_stamp(row["at"]) + INTERVAL if row else None


def is_due(connection: sqlite3.Connection, now: datetime, job: str = JOB) -> bool:
    when = next_due(connection, job)
    return when is None or now >= when


def stored_snapshot(connection: sqlite3.Connection) -> datetime | None:
    row = connection.execute(
        "SELECT value FROM meta WHERE key = ?", (SNAPSHOT_KEY,)).fetchone()
    try:
        return parse_stamp(row["value"]) if row and row["value"] else None
    except ValueError:
        return None


# --- the request -------------------------------------------------------------

@dataclass
class Fetched:
    """What came back: a status, or the reason there was none."""
    status: int | None
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str | None = None


def request(url: str) -> Fetched:
    """One GET. Never raises: a failure is a result, to be logged and shown."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return Fetched(int(response.status), response.read(), headers)
    except urllib.error.HTTPError as exc:
        return Fetched(int(exc.code), b"", {}, f"HTTP {exc.code}")
    except (urllib.error.URLError, http.client.HTTPException, OSError,
            ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return Fetched(None, b"", {}, f"{type(exc).__name__}: {reason}")


def snapshot_time(headers: dict, now: datetime) -> datetime:
    """When the server's copy was made: `Date` minus `Age`.

    The answer comes out of a CDN cache of roughly 240 seconds. Without `Date`
    it is now; without `Age` the copy is taken as fresh.
    """
    try:
        age = max(0, int(headers.get("age", 0)))
    except (TypeError, ValueError):
        age = 0
    moment = now
    if headers.get("date"):
        try:
            moment = email.utils.parsedate_to_datetime(headers["date"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            moment = now
    return (moment - timedelta(seconds=age)).astimezone(timezone.utc).replace(
        microsecond=0)


@dataclass
class Outcome:
    """The network half of one request, ready for `finish`."""
    #: 'ok' (a newer copy, saved), 'stale' (not newer than the stored one) or
    #: 'failed' (nothing usable).
    kind: str
    status: int | None
    detail: str
    snapshot: datetime | None = None
    entries: list | None = None


def save_atomically(path: Path, data: bytes) -> None:
    """Temp file beside the target, then `os.replace`: never half a file."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def fetch_profile(cfg, account_id: str, stored: datetime | None,
                  now: datetime | None = None) -> Outcome:
    """The request, the check, the age comparison and the file. No database.

    Runs on a worker thread. `stored` is the age of the copy already kept.
    """
    now = now or utcnow()
    got = request(mastery.profile_url(account_id))
    if got.status != 200:
        return Outcome("failed", got.status, got.error or f"HTTP {got.status}")
    try:
        entries = mastery.parse(got.body, "The answer")
    except ValueError as exc:
        return Outcome("failed", got.status, f"HTTP 200, but {exc}")
    snapshot = snapshot_time(got.headers, now)
    if stored is not None and snapshot <= stored:
        return Outcome("stale", got.status,
                       "the server's copy is no newer than the one kept "
                       f"(from {stamp(snapshot)[:16]} UTC)", snapshot)
    try:
        save_atomically(profile_path(cfg), got.body)
    except OSError as exc:
        return Outcome("failed", got.status, f"HTTP 200, but could not save it: {exc}")
    return Outcome("ok", got.status, "", snapshot, entries)


# --- applying the answer -----------------------------------------------------

def applied_before(connection: sqlite3.Connection) -> bool:
    """Whether this installation has taken a profile in before."""
    if connection.execute(
            "SELECT 1 FROM meta WHERE key = 'mastery_imported_at'").fetchone():
        return True
    return connection.execute(
        "SELECT 1 FROM owned_items WHERE source = 'profile' AND mastered = 1 "
        "LIMIT 1").fetchone() is not None


def names_line(found: dict, limit: int = 3) -> str:
    names = [name for _unique, name, _xp in found["mastered"]]
    shown = ", ".join(names[:limit])
    more = len(names) - limit
    return f"{shown} (+{more} more)" if more > 0 else shown


def apply_entries(connection: sqlite3.Connection, entries: list) -> dict:
    """Take the entries in: {'kind': 'nothing'|'applied'|'review', 'found', 'message'}.

    The first profile this installation sees is never written on its own:
    'review' leaves the database alone and the page offers the list. Later ones
    are written without asking, as one more source of the same fact.
    """
    found = mastery.resolve(connection, entries)
    count = len(found["mastered"])
    if not count:
        dbq.note_mastery_import(connection)
        return {"kind": "nothing", "found": found,
                "message": f"nothing new — {found['already']} already marked, "
                           f"{found['short']} not yet at their rank cap"}
    if not applied_before(connection):
        return {"kind": "review", "found": found,
                "message": f"{count} item(s) reached their rank cap — review "
                           "them before they are marked"}
    for unique, _name, _xp in found["mastered"]:
        dbq.set_item_mastered(connection, unique, source="profile")
    dbq.note_mastery_import(connection)
    return {"kind": "applied", "found": found,
            "message": f"{count} newly mastered: {names_line(found)}"}


def pending_review(connection: sqlite3.Connection, cfg) -> dict | None:
    """The unreviewed first profile, if the kept file holds one."""
    if applied_before(connection):
        return None
    path = profile_path(cfg)
    if not path.is_file():
        return None
    try:
        found = mastery.resolve(connection, mastery.load(str(path)))
    except ValueError:
        return None
    return found if found["mastered"] else None


def finish(connection: sqlite3.Connection, outcome: Outcome, kind: str,
           at: datetime, row_id: int | None = None) -> dict:
    """The database half, on the GUI thread: apply, remember, log.

    `row_id` is the log row written when the request began (the fetcher writes
    it then, so a crash or a quit before the answer still counts the request
    and the next start does not ask again); without it a row is added now.
    Returns {'kind', 'status', 'message', 'found'} - 'kind' is 'failed',
    'stale', 'nothing', 'applied' or 'review'.
    """
    result = {"kind": outcome.kind, "status": outcome.status,
              "message": outcome.detail, "found": None}
    if outcome.kind == "ok":
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (SNAPSHOT_KEY, stamp(outcome.snapshot)))
        connection.commit()
        applied = apply_entries(connection, outcome.entries or [])
        result.update(applied)
        result["message"] = (f"{applied['message']}; server copy from "
                             f"{stamp(outcome.snapshot)[:16]} UTC")
    if row_id is None:
        record(connection, at, kind, outcome.status, result["message"])
    else:
        settle(connection, row_id, outcome.status, result["message"])
    return result
