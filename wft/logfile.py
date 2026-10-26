"""The tracker's own log, one file per ISO week.

For finding faults on a machine the developer cannot see: the lines the Rewards
page writes to stderr (readings, the hotkey and its device, clicks without a
position, bookings) and unhandled exceptions with their traceback also go to
`<data folder>/logs/YYYY-Www.log`. The 4 newest weeks are kept.

Nothing here may cost a reading or the start: every failure is swallowed, with
at most one line on stderr per run.
"""
from __future__ import annotations

import datetime
import re
import sys
import threading
import time
import traceback
from pathlib import Path

KEEP_WEEKS = 4
_NAME = re.compile(r"^\d{4}-W\d{2}\.log$")

_cfg = None
_lock = threading.Lock()
_complained = False


def log_dir(cfg) -> Path:
    """The folder of the log files; from the same data folder as the database."""
    return cfg.data_dir_path() / "logs"


def week_file_name(ts: float) -> str:
    """`2026-W43.log` for the ISO week of the timestamp (year of the week, so the
    turn of the year can give 2026-W53 and 2027-W01)."""
    iso = datetime.datetime.fromtimestamp(ts).isocalendar()
    year, week = iso[0], iso[1]
    return f"{year:04d}-W{week:02d}.log"


def current_file(cfg, ts: float | None = None) -> Path:
    return log_dir(cfg) / week_file_name(time.time() if ts is None else ts)


def file_line(body: str, ts: float) -> str:
    """`YYYY-MM-DD HH:MM:SS <tag>: <text>`; `body` is the tagged text."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) + " " + body


def _complain(exc: BaseException) -> None:
    global _complained
    if not _complained:
        _complained = True
        try:
            print(f"warning: log file not written: {exc}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def tidy(cfg, keep: int = KEEP_WEEKS) -> None:
    """Delete the log files of older weeks; only names exactly `YYYY-Www.log`."""
    try:
        files = sorted(p for p in log_dir(cfg).iterdir()
                       if _NAME.match(p.name) and p.is_file())
        for old in files[:-keep] if keep > 0 else files:
            old.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        _complain(exc)


def append(cfg, body: str, ts: float) -> None:
    """Append one line to the file of the week of `ts`, flushed at once."""
    try:
        path = current_file(cfg, ts)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(file_line(body, ts) + "\n")
                handle.flush()
    except Exception as exc:  # noqa: BLE001
        _complain(exc)


def enable(cfg) -> None:
    """Start writing for this run: tidy, the start line, the exception hooks.
    Not called by the self-test or by anything that only builds pictures."""
    global _cfg
    _cfg = cfg
    tidy(cfg)
    try:
        from wft import version
        write(f"start: version {version.number()} ({version.describe()})")
    except Exception as exc:  # noqa: BLE001
        _complain(exc)
    previous = sys.excepthook

    def hook(kind, value, tb):
        write("error: unhandled exception\n" + "".join(
            traceback.format_exception(kind, value, tb)).rstrip())
        previous(kind, value, tb)

    sys.excepthook = hook
    old_thread = threading.excepthook

    def thread_hook(args):
        write("error: unhandled exception in thread\n" + "".join(
            traceback.format_exception(args.exc_type, args.exc_value,
                                       args.exc_traceback)).rstrip())
        old_thread(args)

    threading.excepthook = thread_hook


def write(body: str, ts: float | None = None) -> None:
    """Write one tagged line if logging is enabled for this run; else nothing."""
    if _cfg is not None:
        append(_cfg, body, time.time() if ts is None else ts)
