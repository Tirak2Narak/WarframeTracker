"""The clock behind everything the tool fetches by itself.

Not tied to a page: it is built by the application, runs whether or not the
Import page was ever opened, and does one thing - every few minutes it asks
each registered job whether it is due, and starts at most one, never while any
job is still running. A job is any object with `busy`, `due(now)` and
`start("auto")`; the profile fetch is the first, the missing pictures are meant
to be the second, each with its own schedule and its own switch.

The first look comes `FIRST_MS` after the start (not at once: the window
should be up and the start not slowed), then every `EVERY_MS`. A fetch that
was due while the tool was not running is therefore made at the next start.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer

from wft.profile.fetch import utcnow

FIRST_MS = 30_000
EVERY_MS = 10 * 60_000


class Scheduler(QObject):
    def __init__(self, parent=None, clock=utcnow, first_ms: int = FIRST_MS,
                 every_ms: int = EVERY_MS):
        super().__init__(parent)
        self.clock = clock
        self.jobs: list = []
        self._every_ms = every_ms
        self._timer = QTimer(self)
        self._timer.setInterval(first_ms)
        self._timer.timeout.connect(self._on_timer)

    def add(self, job) -> None:
        self.jobs.append(job)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _on_timer(self) -> None:
        self._timer.setInterval(self._every_ms)
        self.tick()

    def tick(self):
        """Start the first due job, unless one is running. Returns it, or None."""
        if any(job.busy for job in self.jobs):
            return None
        now = self.clock()
        for job in self.jobs:
            if job.due(now) and job.start("auto"):
                return job
        return None
