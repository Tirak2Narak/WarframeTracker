"""The profile fetch as an object the Import card and the scheduler share.

One per configuration (`shared`), so a press on the reload button and a due
automatic fetch cannot run at the same time: both ask the same object, which
runs at most one request. The network half runs on a `TaskWorker` thread and
touches no database; the answer is applied on the GUI thread, with a
connection opened for it (`wft.profile.fetch.finish`).

The schedule is not kept here. `due` reads the newest `fetch_log` row, so a new
object after a restart knows exactly what the old one did.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ui.workers.task import TaskWorker
from wft.data import db as dbq
from wft.profile import fetch


class ProfileFetcher(QObject):
    #: A request began; the reload button locks.
    started = Signal()
    #: A request ended: the dict `fetch.finish` returns.
    finished = Signal(object)

    def __init__(self, cfg, clock=fetch.utcnow, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.clock = clock
        self._worker: TaskWorker | None = None
        self._kind = "auto"
        self._at = None
        self._row: int | None = None

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def due(self, now=None) -> bool:
        """An automatic fetch is allowed now: enabled, and 24 hours since the last."""
        if not fetch.enabled(self.cfg):
            return False
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                return fetch.is_due(connection, now or self.clock())
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - a broken database must not fetch
            return False

    def start(self, kind: str) -> bool:
        """Begin one request ('auto' or 'manual'); False when one is running or no id is set."""
        account_id = str(self.cfg.get("profile.account_id", "") or "").strip()
        if self.busy or not fetch.valid_id(account_id):
            return False
        at = self.clock()
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                stored = fetch.stored_snapshot(connection)
                # Logged before it is made: a request that never comes back
                # (a crash, a quit) still counts, and the next start waits.
                self._row = fetch.record(connection, at, kind, None,
                                         "no answer recorded")
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - no database to log in: no request either
            return False
        self._kind = kind
        self._at = at
        worker = TaskWorker(fetch.fetch_profile, self.cfg, account_id, stored,
                            self._at, parent=self)
        worker.finished_ok.connect(self._done)
        worker.failed.connect(self._broke)
        self._worker = worker
        self.started.emit()
        worker.start()
        return True

    def _done(self, outcome) -> None:
        self._settle(outcome)

    def _broke(self, message: str) -> None:
        # Not reachable through `request` (it never raises); here so a bug in the
        # worker still leaves a log row and a free fetcher.
        self._settle(fetch.Outcome("failed", None, message))

    def _settle(self, outcome) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                result = fetch.finish(connection, outcome, self._kind, self._at,
                                      self._row)
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - shown, not raised into the event loop
            result = {"kind": "failed", "status": outcome.status, "found": None,
                      "message": f"the answer could not be recorded: {exc}"}
        self.finished.emit(result)

    def wait(self) -> None:
        """Let a running request end; called as the application quits."""
        worker = self._worker
        if worker is not None:
            worker.wait()


def shared(cfg) -> ProfileFetcher:
    """The one fetcher of this configuration."""
    found = getattr(cfg, "_profile_fetcher", None)
    if found is None:
        found = ProfileFetcher(cfg)
        cfg._profile_fetcher = found
    return found
