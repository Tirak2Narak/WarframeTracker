"""Fetching the official item icons, once.

The previous tool's lists showed the artwork and ours showed text, which is
most of why they read as a database. It got the pictures the way the game
does: `ExportManifest.json` maps every uniqueName to a texture path, and the
picture lives under DE's content host at that path.

Two rules govern how this asks for them, and both are here because of what
happened when they were not followed. The tool once probed a Warframe endpoint
to see how it behaved under load and got the address flagged, which
stopped the *game* logging in. So:

* Only what is needed. Nineteen thousand icons exist; a few hundred belong to
  things this user holds. The rest are never requested.
* Once, and slowly. The pictures are stored in the database, a later run asks
  only for what is missing, and there is a pause between requests.

The tool starts a run by itself (`[pictures] auto_fetch`): after an
applied inventory import, and otherwise at most once in 24 hours, never when
nothing is missing. Each run is one row in `fetch_log` (job `pictures`), written
when it begins and filled in when it ends. A picture the server does not have
(404) is skipped; any other failure ends the run, because retrying against a
server that has refused or cannot be reached is exactly what must not happen.
"""
from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from typing import Callable

from wft.data import db as dbq
from wft.data.publicexport import USER_AGENT
from wft.profile import fetch as log

#: The `fetch_log.job` of this fetch.
JOB = "pictures"

#: Where a texture path hangs off. The path from the manifest already begins
#: with a slash and already carries its own content hash.
CONTENT_BASE = "http://content.warframe.com/PublicExport"

#: Seconds between requests. Not a rate limit anyone published - a courtesy,
#: and cheap insurance: four hundred icons at this pace is a minute, and a
#: minute is nothing against the cost of being wrong about it.
PAUSE = 0.12

TIMEOUT = 30

#: How wide a stored icon is. The manifest icons are square and about 256px;
#: this is the size a list actually draws at the largest text setting, and
#: storing more is storing something nobody will see.
WIDTH = 128


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _shrink(payload: bytes) -> bytes | None:
    """Scale an icon down and re-encode it, keeping transparency."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        return None
    if image.shape[1] > WIDTH:
        scale = WIDTH / image.shape[1]
        image = cv2.resize(image, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA)
    # PNG rather than JPEG: these have an alpha channel and a white box behind
    # a Warframe silhouette is worse than no picture at all.
    ok, buffer = cv2.imencode(".png", image)
    return buffer.tobytes() if ok else None


def fetch_missing(connection, progress: Callable[[int, int, str], None] | None = None,
                  cancel: Callable[[], bool] | None = None,
                  limit: int | None = None) -> dict:
    """Fetch icons for everything held that has none stored yet.

    Returns counts rather than raising. 'fetched' arrived, 'gone' are pictures
    the server answered 404 for (a few icons are not on DE's host; skipped, the
    run goes on), 'failed' came back unreadable, 'pending' is what was asked
    for. Any other failure - no connection, a timeout, 403, 429, 5xx - ends the
    run at once: 'stopped' then holds the reason ('HTTP 503', 'URLError: ...')
    and 'stop_status' the code if there was one. 'cancelled' is true when the
    caller's `cancel` ended it.
    """
    pending = dbq.missing_pictures(connection)
    if limit:
        pending = pending[:limit]
    got = failed = gone = 0
    stopped = stop_status = None
    cancelled = False
    for position, row in enumerate(pending, start=1):
        if cancel is not None and cancel():
            cancelled = True
            break
        if progress:
            progress(position, len(pending), row["unique_name"])
        try:
            payload = _fetch(CONTENT_BASE + row["location"])
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                gone += 1
                time.sleep(PAUSE)
                continue
            stopped, stop_status = f"HTTP {exc.code}", int(exc.code)
            break
        except (urllib.error.URLError, http.client.HTTPException, OSError,
                ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            stopped = f"{type(exc).__name__}: {reason}"
            break
        small = _shrink(payload)
        if small:
            dbq.set_thumbnail(connection, row["kind"], row["unique_name"],
                              small, source="export")
            got += 1
        else:
            failed += 1
        if position % 25 == 0:
            connection.commit()
        time.sleep(PAUSE)
    connection.commit()
    return {"fetched": got, "failed": failed, "gone": gone,
            "pending": len(pending), "stopped": stopped,
            "stop_status": stop_status, "cancelled": cancelled}


#: How `summary` marks a run the server ended; `refused_recently` looks for it.
STOPPED = "; stopped: "


def summary(counts: dict) -> str:
    """One line for the log and the card: what arrived and how it ended."""
    parts = [f"{counts.get('fetched', 0)} fetched"]
    if counts.get("gone"):
        parts.append(f"{counts['gone']} missing on the server")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} could not be read")
    text = ", ".join(parts)
    if counts.get("stopped"):
        text += f"{STOPPED}{counts['stopped']}"
    elif counts.get("cancelled"):
        text += "; stopped by you"
    return text


# --- the schedule -----------------------------------------------------

def enabled(cfg) -> bool:
    return bool(cfg.get("pictures.auto_fetch", True))


def is_due(connection, now) -> bool:
    """24 hours since the newest run of any kind, or no run yet."""
    return log.is_due(connection, now, JOB)


def refused_recently(connection, now) -> bool:
    """The newest run was ended by the server or the network less than 24 h ago.

    Then an applied import does not start a run either: the day's wait after a
    refusal holds for every run the tool starts by itself, only the button
    asks anyway.
    """
    row = log.last(connection, JOB)
    if row is None or STOPPED not in (row["detail"] or ""):
        return False
    return now < log.parse_stamp(row["at"]) + log.INTERVAL


def begin(connection, at, kind: str) -> int | None:
    """Log a run as it starts; None (and no row) when no picture is missing."""
    if not dbq.missing_pictures(connection):
        return None
    return log.record(connection, at, kind, None, "no answer recorded", JOB)


def end(connection, row_id: int, counts: dict) -> str:
    """Fill in the row `begin` wrote; returns the line it holds."""
    text = summary(counts)
    log.settle(connection, row_id, counts.get("stop_status"), text)
    return text
