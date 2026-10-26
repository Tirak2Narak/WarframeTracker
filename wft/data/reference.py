"""The reference data update by itself, once a day.

The update is one sequence (`ui.views.settings_view._run_update`, the same as
`wf update`): DE's manifest index, the manifests that changed, a rebuild, the
drop-table page. The tool also starts it by itself
(`[update] auto_fetch`), at most once in 24 hours, and only when reference
data is already there - the very first download stays the Setup page's button.

Each run is one row in `fetch_log` (job `reference`), written when it begins and
filled in when it ends, like the profile and the pictures. The schedule is not
kept in memory: `next_due` reads the newest row, whatever its outcome (a failed
run waits the day too), and the build's own `last_update_at` stamp, so a
`wf update` from the shell counts as well.

No Qt in here: `wf doctor` reads the same functions.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from wft.profile import fetch as log

#: The `fetch_log.job` of this update.
JOB = "reference"

BEGUN = "no answer recorded"
#: How the detail of a run that went through ends; `is_ok` looks for it.
OK_TAIL = "vault status refreshed"


def enabled(cfg) -> bool:
    return bool(cfg.get("update.auto_fetch", True))


def has_data(connection: sqlite3.Connection) -> bool:
    """A build has run here: its counts row is in `meta`."""
    row = connection.execute(
        "SELECT 1 FROM meta WHERE key = 'counts' AND value != ''").fetchone()
    return row is not None


def built_at(connection: sqlite3.Connection) -> datetime | None:
    """When the build last finished (UTC), whoever ran it, or None."""
    row = connection.execute(
        "SELECT value FROM meta WHERE key = 'last_update_at'").fetchone()
    try:
        moment = datetime.fromisoformat(row["value"]) if row else None
    except (ValueError, TypeError):
        return None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def last_run(connection: sqlite3.Connection):
    """The newest row of this job, or None."""
    return log.last(connection, JOB)


def next_due(connection: sqlite3.Connection) -> datetime | None:
    """When the next automatic run may come; None when nothing has ever run.

    24 hours after the newest of: a logged run (failed ones too) and the build's
    own stamp.
    """
    moments = []
    row = log.last(connection, JOB)
    if row is not None:
        moments.append(log.parse_stamp(row["at"]))
    built = built_at(connection)
    if built is not None:
        moments.append(built)
    return max(moments) + log.INTERVAL if moments else None


def is_due(connection: sqlite3.Connection, now: datetime) -> bool:
    """Reference data is there and 24 hours have passed since the last run or build."""
    if not has_data(connection):
        return False
    when = next_due(connection)
    return when is None or now >= when


def begin(connection: sqlite3.Connection, at: datetime, kind: str) -> int:
    """Log a run as it starts. kind: 'auto', 'manual' or 'force'."""
    return log.record(connection, at, kind, None, BEGUN, JOB)


def end(connection: sqlite3.Connection, row_id: int, status: int | None,
        detail: str) -> None:
    log.settle(connection, row_id, status, detail)


def success_text(result) -> str:
    """What a finished run says: manifests downloaded or unchanged, vault status."""
    downloaded = result.get("downloaded") if isinstance(result, dict) else None
    if downloaded is None:
        head = "updated"
    elif downloaded:
        head = f"{downloaded} manifest{'s' if downloaded != 1 else ''} downloaded"
    else:
        head = "manifests unchanged"
    return f"{head}, {OK_TAIL}"


def is_ok(row) -> bool:
    """The row is a run that went through (not one that failed or never answered)."""
    return (row["detail"] or "").endswith(OK_TAIL)


def error_status(exc: BaseException | None) -> int | None:
    """The HTTP code an exception (or what it was raised from) carries, else None."""
    seen = 0
    while exc is not None and seen < 5:
        code = getattr(exc, "code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None
