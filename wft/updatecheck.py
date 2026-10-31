"""Looking for a newer release of the tool, once a day at most.

One GET on the release address in the config (`updates.url`, GitHub's "latest
release" of the public repository): the tag and the release page come back, and
that is all that is read. Nothing is downloaded or installed; the tool only says
that a newer version exists and where to find it. The rules are those of the
profile fetch and hold here for the same reason:

- one request, no second attempt, the tool's user agent, `fetch.TIMEOUT`;
- at most 1 automatic request per `fetch.INTERVAL`, counted from the newest
  request of any kind (the button counts too), a failed one included, so a 403,
  a 429 or a missing release is answered with a day's wait, not another try;
- every request is a row in `fetch_log` (job `update`), which is also where the
  schedule is read from, so it survives a restart and each user counts for
  himself;
- a failure is a result, never an exception: no network, a refusal, no release
  published yet (404) and an answer that is not the expected JSON all end in
  `kind == "failed"` and change nothing.

No Qt here. The network half (`check`) touches no database; the database half
(`finish`) runs on the GUI thread, like the profile fetch.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from wft import whatsnew
from wft.profile import fetch

JOB = "update"
LATEST_KEY = "update_latest_version"
URL_KEY = "update_latest_url"

_TAG = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")


def url(cfg) -> str:
    return str(cfg.get("updates.url", "") or "").strip()


def enabled(cfg) -> bool:
    """The automatic check: the switch is on and there is an address to ask."""
    return bool(cfg.get("updates.auto_check", True)) and bool(url(cfg))


def parse(body: bytes) -> tuple[str, str]:
    """(version, release page) out of the answer; ValueError when it is not one."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("the answer is not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("the answer is not a release")
    match = _TAG.match(str(data.get("tag_name", "")).strip())
    if match is None:
        raise ValueError("the release has no version number as its tag")
    page = str(data.get("html_url", "")).strip()
    if not page.startswith("https://"):
        raise ValueError("the release has no web address")
    return match.group(1), page


def is_newer(latest: str, current: str) -> bool:
    try:
        return whatsnew.is_newer(latest, current)
    except ValueError:
        return False


def check(cfg, current: str) -> dict:
    """The request and the comparison. Never raises, no database.

    {'kind': 'newer'|'current'|'failed', 'status', 'version', 'url', 'message'}
    """
    got = fetch.request(url(cfg))
    if got.status != 200:
        message = got.error or f"HTTP {got.status}"
        if got.status == 404:
            message = "no release is published yet"
        return {"kind": "failed", "status": got.status, "version": "",
                "url": "", "message": message}
    try:
        version, page = parse(got.body)
    except ValueError as exc:
        return {"kind": "failed", "status": got.status, "version": "",
                "url": "", "message": str(exc)}
    if is_newer(version, current):
        return {"kind": "newer", "status": got.status, "version": version,
                "url": page, "message": f"version {version} is available"}
    return {"kind": "current", "status": got.status, "version": version,
            "url": page, "message": f"up to date (latest release {version})"}


def finish(connection: sqlite3.Connection, result: dict, kind: str,
           at: datetime, row_id: int | None = None) -> dict:
    """Remember what the answer said and log the request.

    A newer release is kept in `meta` so the notice survives a restart; an
    answer that is not newer clears it (the installed version caught up); a
    failure leaves it as it was.
    """
    if result["kind"] == "newer":
        for key, value in ((LATEST_KEY, result["version"]), (URL_KEY, result["url"])):
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        connection.commit()
    elif result["kind"] == "current":
        connection.execute("DELETE FROM meta WHERE key IN (?, ?)", (LATEST_KEY, URL_KEY))
        connection.commit()
    if row_id is None:
        fetch.record(connection, at, kind, result["status"], result["message"], JOB)
    else:
        fetch.settle(connection, row_id, result["status"], result["message"])
    return result


def available(connection: sqlite3.Connection, current: str) -> tuple[str, str] | None:
    """(version, release page) of a newer release already found, else None."""
    rows = dict(connection.execute(
        "SELECT key, value FROM meta WHERE key IN (?, ?)", (LATEST_KEY, URL_KEY)))
    version, page = rows.get(LATEST_KEY), rows.get(URL_KEY)
    if version and page and is_newer(version, current):
        return version, page
    return None


def is_due(connection: sqlite3.Connection, now: datetime) -> bool:
    return fetch.is_due(connection, now, JOB)


def last(connection: sqlite3.Connection):
    return fetch.last(connection, JOB)
