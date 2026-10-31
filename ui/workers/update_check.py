"""The update check as an object the Settings page, the banner and the clock share.

Same shape as `ProfileFetcher`: one per configuration (`shared`), so a press on
the button and a due automatic check cannot run together; the request runs on a
`TaskWorker` thread without a database, the answer is applied on the GUI thread.
The schedule is not kept here: `due` reads the newest `fetch_log` row.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ui.workers.task import TaskWorker
from wft import updatecheck, version as version_mod
from wft.data import db as dbq
from wft.profile import fetch


class UpdateChecker(QObject):
    started = Signal()
    #: The dict `updatecheck.finish` returns, plus 'manual' (True for the button).
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
        """An automatic check is allowed now: switched on, 24 hours since the last."""
        if not updatecheck.enabled(self.cfg):
            return False
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                return updatecheck.is_due(connection, now or self.clock())
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - a broken database must not ask anyone
            return False

    def start(self, kind: str) -> bool:
        """Begin one request ('auto' or 'manual'); False when one runs or nothing can be asked."""
        if self.busy or not updatecheck.url(self.cfg):
            return False
        at = self.clock()
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                self._row = fetch.record(connection, at, kind, None,
                                         "no answer recorded", updatecheck.JOB)
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - nowhere to log: no request either
            return False
        self._kind, self._at = kind, at
        worker = TaskWorker(updatecheck.check, self.cfg, version_mod.number(),
                            parent=self)
        worker.finished_ok.connect(self._settle)
        worker.failed.connect(lambda message: self._settle(
            {"kind": "failed", "status": None, "version": "", "url": "",
             "message": message}))
        self._worker = worker
        self.started.emit()
        worker.start()
        return True

    def _settle(self, result) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                result = updatecheck.finish(connection, result, self._kind,
                                            self._at, self._row)
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - shown, not raised into the event loop
            result = {"kind": "failed", "status": None, "version": "", "url": "",
                      "message": f"the answer could not be recorded: {exc}"}
        result = dict(result, manual=self._kind == "manual")
        self.finished.emit(result)

    def wait(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.wait()


def shared(cfg) -> UpdateChecker:
    found = getattr(cfg, "_update_checker", None)
    if found is None:
        found = UpdateChecker(cfg)
        cfg._update_checker = found
    return found
