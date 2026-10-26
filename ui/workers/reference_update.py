"""The reference data update as a job for the clock.

The run itself is `UpdateJob`, the one object the Settings button, the Setup
button and this share, so a run started by any of them keeps the others out
(`busy`). This adds only the schedule: `due` reads `fetch_log` and the build's
own stamp through `wft.data.reference`, so a new object after a restart knows
what the old one did, and so does a `wf update` made in a shell.
"""
from __future__ import annotations

from wft.data import db as dbq
from wft.data import reference
from wft.profile import fetch


class ScheduledReference:
    def __init__(self, cfg, job, clock=fetch.utcnow):
        self.cfg = cfg
        self.job = job
        self.clock = clock

    @property
    def busy(self) -> bool:
        return self.job.running

    def due(self, now=None) -> bool:
        """An automatic run is allowed now: switched on, data present, 24 hours since."""
        if not reference.enabled(self.cfg):
            return False
        if not self.cfg.db_path.is_file():
            return False
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                return reference.is_due(connection, now or self.clock())
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - a broken database must not download
            return False

    def start(self, kind: str) -> bool:
        # By name: the first positional argument of `UpdateJob.start` is `force`.
        return self.job.start(force=False, kind=kind)

    def wait(self) -> None:
        """Let a running update end; called as the application quits."""
        self.job.wait(15_000)
