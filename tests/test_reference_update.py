#!/usr/bin/env python3
"""The reference data update by itself, in wft/data/reference.py.

Nothing here reaches a server: `pe._fetch` and `vaulted._download` raise, and
the steps between them are replaced by counting stand-ins. The schedule is
checked on a made-up clock against a database of its own; the run log through
`UpdateJob` with an event loop, because the row is written on the GUI thread.

Run: python3 tests/test_reference_update.py
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="wft-config-")
# Even a config that forgot to name its data folder lands in a scratch one.
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="wft-data-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.views import settings_view  # noqa: E402
from ui.workers.reference_update import ScheduledReference  # noqa: E402
from wft import config as config_mod  # noqa: E402
from wft import doctor  # noqa: E402
from wft.data import build as build_mod  # noqa: E402
from wft.data import db as dbq  # noqa: E402
from wft.data import publicexport as pe  # noqa: E402
from wft.data import reference  # noqa: E402
from wft.data import vaulted as vaulted_mod  # noqa: E402
from wft.profile import fetch  # noqa: E402

app = QApplication.instance() or QApplication([])
problems: list[str] = []


def check(ok, text):
    if not ok:
        problems.append(text)


T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

# --- no network, ever -----------------------------------------------------------
calls: list[str] = []


def no_net(*_args, **_kwargs):
    calls.append("network")
    raise OSError("offline in tests")


pe._fetch = no_net
vaulted_mod._download = no_net

#: What the stand-ins for the steps do; set per case.
steps = {"downloaded": 3, "fail": None, "gate": None}
seen: list = []


def fake_index(cfg):
    seen.append("index")
    if steps["fail"] == "index":
        raise urllib.error.HTTPError("x", 503, "unavailable", {}, None)
    return {"a": object(), "b": object(), "c": object()}


def fake_outdated(cfg, index):
    return list(index.values())[:steps["downloaded"]]


def fake_download(cfg, targets, progress=None):
    seen.append(("download", len(targets)))


def fake_build(cfg, notes):
    seen.append("build")
    write_data(cfg, datetime.now(timezone.utc))
    return {"items": 1}


def fake_apply(cfg, notes):
    seen.append("vault")
    if steps["fail"] == "vault":
        raise vaulted_mod.DropTableError("download of x failed: timed out")
    return {"relics": 1}


pe.fetch_index, pe.outdated, pe.download = fake_index, fake_outdated, fake_download
build_mod.build = fake_build
vaulted_mod.apply = fake_apply


def write_data(cfg, built):
    """What a finished build leaves in `meta`."""
    conn = dbq.connect(cfg.db_path)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('counts', '{\"items\": 1}')")
    if built is not None:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_update_at', ?)",
                     (built.isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()


def make(*, data=True, built=None, auto=True):
    cfg = config_mod.load()
    cfg.override("paths.data_dir", tempfile.mkdtemp())
    cfg.override("update.auto_fetch", auto)
    conn = dbq.connect(cfg.db_path)
    if data:
        write_data(cfg, built)
    return cfg, conn


def due(cfg, conn, at):
    return reference.enabled(cfg) and reference.is_due(conn, at)


# --- 1. the schedule ---------------------------------------------------------------
cfg, conn = make()
check(due(cfg, conn, T0), "data present, never run, no stamp: not due")
row = reference.begin(conn, T0, "auto")
check(not due(cfg, conn, T0 + timedelta(hours=23)), "due 23 h after a run")
check(due(cfg, conn, T0 + timedelta(hours=25)), "not due 25 h after a run")
reference.end(conn, row, None, reference.success_text({"downloaded": 0}))
check(not due(cfg, conn, T0 + timedelta(hours=23)), "due 23 h after a settled run")

# a failed run waits the day too
cfg, conn = make()
row = reference.begin(conn, T0, "auto")
reference.end(conn, row, 503, "HTTPError: HTTP Error 503: unavailable")
check(not due(cfg, conn, T0 + timedelta(hours=23)), "due 23 h after a failed run")
check(due(cfg, conn, T0 + timedelta(hours=25)), "not due 25 h after a failed run")

# no reference data: never due, however old the log
cfg, conn = make(data=False)
check(not due(cfg, conn, T0 + timedelta(days=400)), "no reference data: due")
reference.begin(conn, T0, "manual")
check(not due(cfg, conn, T0 + timedelta(days=400)), "no reference data, old log: due")
cfg, conn = make(data=False)
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_update_at', ?)",
             (T0.isoformat(),))
conn.commit()
check(not due(cfg, conn, T0 + timedelta(days=400)), "a stamp without a counts row: due")

# a newer build stamp (a `wf update` from the shell) counts
cfg, conn = make(built=T0 + timedelta(hours=20))
row = reference.begin(conn, T0, "auto")
check(not due(cfg, conn, T0 + timedelta(hours=40)),
      "20 h-newer build stamp ignored: due 20 h after it")
check(due(cfg, conn, T0 + timedelta(hours=45)), "25 h after the build stamp: not due")
# an old build stamp does not hold a newer run back
cfg, conn = make(built=T0 - timedelta(days=7))
check(due(cfg, conn, T0), "a 7-day-old stamp: not due")
reference.begin(conn, T0, "auto")
check(not due(cfg, conn, T0 + timedelta(hours=23)), "old stamp, run 23 h ago: due")
# a stamp without a zone is read as UTC
cfg, conn = make(built=None)
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_update_at', '2026-10-24T12:00:00')")
conn.commit()
check(not due(cfg, conn, T0 + timedelta(hours=23)) and due(cfg, conn, T0 + timedelta(hours=25)),
      "a naive stamp is not read as UTC")

# the switch
cfg, conn = make()
cfg.override("update.auto_fetch", False)
check(not due(cfg, conn, T0 + timedelta(days=500)), "switch off: due")
cfg.override("update.auto_fetch", True)

# a connection of its own (a restart) sees the same schedule
cfg, conn = make()
reference.begin(conn, T0, "manual")
other = dbq.connect(cfg.db_path)
check(not reference.is_due(other, T0 + timedelta(hours=23))
      and reference.is_due(other, T0 + timedelta(hours=25)), "a new connection does not know the log")
check(reference.next_due(other) == T0 + fetch.INTERVAL, f"next_due {reference.next_due(other)}")

# the scheduled job: switch, data and a broken database
cfg, conn = make()
job = settings_view.UpdateJob(cfg)
scheduled = ScheduledReference(cfg, job)
check(scheduled.due(T0) and not scheduled.busy, "scheduled: fresh data, not due")
cfg.override("update.auto_fetch", False)
check(not scheduled.due(T0), "scheduled: due with the switch off")
cfg.override("update.auto_fetch", True)
cfg2, conn2 = make(data=False)
check(not ScheduledReference(cfg2, job).due(T0), "scheduled: due without data")
cfg3 = config_mod.load()
cfg3.override("paths.data_dir", tempfile.mkdtemp())
check(not ScheduledReference(cfg3, job).due(T0) and not cfg3.db_path.is_file(),
      "scheduled: a missing database was created or counted as due")
cfg4 = config_mod.load()
cfg4.override("paths.data_dir", tempfile.mkdtemp())
cfg4.db_path.write_bytes(b"this is not a database" * 50)
check(not ScheduledReference(cfg4, job).due(T0), "scheduled: a broken database is due")


# --- 2. the run log through UpdateJob ---------------------------------------------------
def wait_for(job, seconds=10):
    end = time.time() + seconds
    while job.running and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    for _ in range(5):
        app.processEvents()


def rows(cfg):
    c = dbq.connect(cfg.db_path)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM fetch_log WHERE job='reference' ORDER BY id")]
    finally:
        c.close()


import threading  # noqa: E402

cfg, conn = make()
job = settings_view.UpdateJob(cfg)
gate = threading.Event()
real_build = build_mod.build


def held_build(cfg_, notes):
    gate.wait(10)
    return fake_build(cfg_, notes)


build_mod.build = held_build
seen.clear()
steps.update(downloaded=3, fail=None)
check(job.start(kind="auto"), "job: start refused")
app.processEvents()
mid = rows(cfg)
check(len(mid) == 1 and mid[0]["kind"] == "auto" and mid[0]["status"] is None
      and mid[0]["detail"] == "no answer recorded", f"row at the start: {mid}")
check(not job.start(kind="manual") and len(rows(cfg)) == 1, "job: a second run started or logged")
scheduled = ScheduledReference(cfg, job)
check(scheduled.busy, "scheduled: a running job is not busy")
gate.set()
wait_for(job)
done = rows(cfg)
check(len(done) == 1 and done[0]["detail"] == "3 manifests downloaded, vault status refreshed"
      and done[0]["status"] is None, f"row at the end: {done}")
build_mod.build = fake_build

# unchanged manifests
steps.update(downloaded=0)
check(job.start(), "job: start refused (manual)")
wait_for(job)
r = rows(cfg)[-1]
check(r["kind"] == "manual" and r["detail"] == "manifests unchanged, vault status refreshed", r)
# the forced one
steps.update(downloaded=3)
check(job.start(force=True), "job: forced start refused")
wait_for(job)
r = rows(cfg)[-1]
check(r["kind"] == "force", r)

# start("auto") through the scheduler's adapter never forces
forced: list = []
real_run = settings_view._run_update


def spy(cfg_, *, force=False, report=lambda _m: None):
    forced.append(force)
    return real_run(cfg_, force=force, report=report)


settings_view._run_update = spy
check(ScheduledReference(cfg, job).start("auto"), "adapter: start refused")
wait_for(job)
check(forced == [False] and rows(cfg)[-1]["kind"] == "auto",
      f"adapter: force={forced}, kind={rows(cfg)[-1]['kind']}")
settings_view._run_update = real_run

# a failure carries the HTTP code and the message; the next automatic run waits
steps.update(fail="index")
before = len(rows(cfg))
check(job.start(kind="auto"), "job: start refused (failing)")
with contextlib.redirect_stderr(io.StringIO()):  # the worker prints its traceback
    wait_for(job)
r = rows(cfg)[-1]
check(len(rows(cfg)) == before + 1 and r["status"] == 503 and "503" in r["detail"]
      and not reference.is_ok(r), f"failed row: {r}")
check(not reference.is_due(dbq.connect(cfg.db_path), datetime.now(timezone.utc) + timedelta(hours=23)),
      "due 23 h after a failed run")
# the vault step fails after the build: the message, and the code from its cause
steps.update(fail="vault")
check(job.start(kind="auto"), "job: start refused (vault)")
with contextlib.redirect_stderr(io.StringIO()):
    wait_for(job)
r = rows(cfg)[-1]
check(r["status"] is None and "DropTableError" in r["detail"], f"vault failure row: {r}")
check(isinstance(reference.error_status(
    vaulted_mod.DropTableError("x")), type(None)), "error_status: a code from nowhere")
try:
    try:
        raise urllib.error.HTTPError("x", 429, "slow down", {}, None)
    except OSError as inner:
        raise vaulted_mod.DropTableError("download failed") from inner
except vaulted_mod.DropTableError as chained:
    check(reference.error_status(chained) == 429, "error_status: the cause's code")
steps.update(fail=None)

# logging that cannot be written never stops the update
bad = config_mod.load()
bad.override("paths.data_dir", tempfile.mkdtemp())
bad_job = settings_view.UpdateJob(bad)
real_begin = reference.begin


def broken(*_a, **_k):
    raise RuntimeError("log is broken")


reference.begin = broken
seen.clear()
finished: list = []
bad_job.finished.connect(lambda ok, msg: finished.append((ok, msg)))
check(bad_job.start(kind="auto"), "job: a broken log stopped the start")
wait_for(bad_job)
reference.begin = real_begin
check(finished == [(True, "")] and "build" in seen, f"a broken log: {finished} {seen}")

check(not calls, f"a request left the test: {calls}")

# --- 3. wf doctor ------------------------------------------------------------------------
cfg, conn = make()
line = doctor._reference_update_line(cfg)
check(line == "on; never run", f"doctor, never run: {line!r}")
row = reference.begin(conn, T0, "auto")
reference.end(conn, row, None, "3 manifests downloaded, vault status refreshed")
line = doctor._reference_update_line(cfg)
check(line == "on; last 2026-10-24 12:00:00 UTC (auto): 3 manifests downloaded, "
      "vault status refreshed", f"doctor, after a run: {line!r}")
cfg.override("update.auto_fetch", False)
check(doctor._reference_update_line(cfg).startswith("off (switched off); last"), "doctor, switch off")
cfg.override("paths.data_dir", tempfile.mkdtemp())
check(doctor._reference_update_line(cfg) == "off (switched off); no database yet"
      and not cfg.db_path.is_file(), "doctor created a database or missed its absence")

print("reference update   :", "OK" if not problems else f"{len(problems)} problem(s)")
for text in problems:
    print("   ", text)
sys.exit(1 if problems else 0)
