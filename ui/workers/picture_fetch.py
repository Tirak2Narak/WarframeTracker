"""The picture fetch as an object the Import card, the scheduler and the import share.

One per configuration (`shared`), so the button, a due automatic run and the
run that follows an applied import cannot overlap: all three ask this object,
which runs at most one. The network half runs on a `TaskWorker` thread with a
database connection of its own (a sqlite connection belongs to the thread that
made it); the log row is written and filled in on the GUI thread, like the
profile fetch (`ui.workers.profile_fetch`).

The schedule is not kept here: `due` reads the newest `fetch_log` row of the job
`pictures`, so a new object after a restart knows what the old one did.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ui.workers.task import TaskWorker
from wft.data import db as dbq
from wft.data import pictures
from wft.profile import fetch


class PictureFetcher(QObject):
    #: A run began.
    started = Signal()
    #: Progress text "done/total" from the worker.
    progress = Signal(str)
    #: A run ended: {'counts': dict, 'message': str}.
    finished = Signal(object)

    def __init__(self, cfg, clock=fetch.utcnow, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.clock = clock
        self._worker: TaskWorker | None = None
        self._row: int | None = None

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def due(self, now=None) -> bool:
        """An automatic run is allowed now: switched on, and 24 hours since the last."""
        if not pictures.enabled(self.cfg):
            return False
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                return pictures.is_due(connection, now or self.clock())
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - a broken database must not fetch
            return False

    def start(self, kind: str) -> bool:
        """Begin one run ('auto', 'import' or 'manual').

        False when one is running, nothing is missing, or (for 'import') the
        server ended a run less than 24 hours ago - then nothing is requested
        and nothing is logged.
        """
        if self.busy:
            return False
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                if kind == "import" and pictures.refused_recently(
                        connection, self.clock()):
                    return False
                # Logged before the first request: a run that never comes back
                # (a crash, a quit) still counts, and the next start waits.
                self._row = pictures.begin(connection, self.clock(), kind)
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - no database to log in: no request either
            return False
        if self._row is None:
            return False
        worker = TaskWorker(self._run, parent=self)
        worker.progress.connect(self.progress)
        worker.finished_ok.connect(self._done)
        worker.failed.connect(self._broke)
        self._worker = worker
        self.started.emit()
        worker.start()
        return True

    def _run(self, report):
        connection = dbq.connect(self.cfg.db_path)
        try:
            return pictures.fetch_missing(
                connection,
                progress=lambda i, total, _name: report(f"{i}/{total}"),
                cancel=lambda: self._worker is not None
                and self._worker.cancelled)
        finally:
            connection.close()

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancelled = True

    def _done(self, counts) -> None:
        self._settle(counts or {})

    def _broke(self, message: str) -> None:
        self._settle({"fetched": 0, "stopped": message})

    def _settle(self, counts: dict) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                message = pictures.end(connection, self._row, counts)
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - shown, not raised into the event loop
            message = f"the result could not be recorded: {exc}"
        # Lists that already drew a placeholder draw the picture next time.
        try:
            from ui.widgets import thumbs

            thumbs.clear()
        except Exception:  # noqa: BLE001
            pass
        self.finished.emit({"counts": counts, "message": message})

    def wait(self) -> None:
        """Let a running run end; called as the application quits."""
        worker = self._worker
        if worker is not None:
            worker.cancelled = True
            worker.wait()


def shared(cfg) -> PictureFetcher:
    """The one fetcher of this configuration."""
    found = getattr(cfg, "_picture_fetcher", None)
    if found is None:
        found = PictureFetcher(cfg)
        cfg._picture_fetcher = found
    return found
