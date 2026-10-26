"""A generic background task, so every long call gets its own QThread for free.

The alternative — a bespoke QThread subclass per job — is what this avoids:
`update`, `scan` and the relic trigger all need the same three signals
(progress, finished, failed), so the shape is written once here.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Signal


class TaskWorker(QThread):
    """Runs one callable on a worker thread.

    The callable receives a `report(message)` function it can call to post
    progress text back to the GUI thread. It returns its result, which is
    forwarded through `finished_ok`; an exception is forwarded through
    `failed` instead of crashing the thread silently.
    """

    progress = Signal(str)
    #: The progress object itself, for callers that want more than a sentence -
    #: a bar needs a fraction, not a description of one.
    progress_value = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)

    #: Set by the view to ask the task to stop. The task decides when to look.
    cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __init__(self, fn: Callable[..., Any], *args, parent=None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        # Which of the optional arguments this callable actually takes, decided
        # by looking at it rather than by trying and catching.
        #
        # It used to call with all of them and treat TypeError as "does not
        # accept report" - which also catches a TypeError raised *inside* the
        # call, two minutes deep, and then runs the whole thing again without
        # report, value or cancel. The second run reported no progress, ignored
        # the cancel button and took another three minutes: from the outside,
        # indistinguishable from a hang. Every "it hung again" in this session
        # was this.
        extras = {}
        try:
            import inspect

            takes = inspect.signature(self._fn).parameters
            accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD
                              for p in takes.values())
            for name, value in (("report", self.progress.emit),
                                ("value", self.progress_value.emit),
                                ("cancel", lambda: self.cancelled)):
                if accepts_any or name in takes:
                    extras[name] = value
        except (TypeError, ValueError):
            # Not introspectable - a builtin or a C function. Call it plainly.
            extras = {}

        try:
            result = self._fn(*self._args, **extras, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - surface every failure to the UI
            import traceback

            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished_ok.emit(result)
