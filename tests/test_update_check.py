#!/usr/bin/env python3
"""The check for a newer release (wft/updatecheck.py, ui/workers/update_check.py).

`urllib.request.urlopen` is replaced for the whole run: nothing here reaches a
server, and each case counts how often the request was made, because "at most
one in 24 hours, none after a failure" is the rule this feature lives by.

Run: python3 tests/test_update_check.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="wft-config-")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="wft-data-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from wft import config as config_mod  # noqa: E402
from wft import updatecheck  # noqa: E402
from wft.data import db as dbq  # noqa: E402
from wft.profile import fetch  # noqa: E402

problems: list[str] = []


def check(ok, text):
    if not ok:
        problems.append(text)


T0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
calls: list[dict] = []
script: list = []


class _Response:
    status = 200

    def __init__(self, body):
        self._body = body
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(req, timeout=None, **_kw):
    calls.append({"url": req.full_url, "ua": req.get_header("User-agent"),
                  "timeout": timeout})
    step = script.pop(0)
    if step[0] == "http":
        raise urllib.error.HTTPError(req.full_url, step[1], "x", {}, None)
    if step[0] == "net":
        raise urllib.error.URLError(step[1])
    return _Response(step[1])


urllib.request.urlopen = fake_urlopen


def release(tag, page="https://github.com/example/repo/releases/tag/x"):
    return json.dumps({"tag_name": tag, "html_url": page}).encode()


def make(*, auto=True, address="https://api.example.invalid/releases/latest"):
    directory = pathlib.Path(tempfile.mkdtemp())
    cfg = config_mod.load()
    cfg.override("paths.data_dir", str(directory))
    cfg.override("updates.auto_check", auto)
    cfg.override("updates.url", address)
    return cfg, dbq.connect(cfg.db_path)


def ask(cfg, step, current="1.5.1"):
    script.append(step)
    return updatecheck.check(cfg, current)


# --- 0. the shipped defaults: on, and the public repository's latest release ----
default = config_mod.load()
check(default.get("updates.auto_check") is True, "the check is not on by default")
check(str(default.get("updates.url", "")).startswith("https://api.github.com/repos/")
      and str(default.get("updates.url", "")).endswith("/releases/latest"),
      f"default address: {default.get('updates.url')!r}")

# --- 1. newer, equal, older ---------------------------------------------------
cfg, conn = make()
r = ask(cfg, ("ok", release("v1.6")))
check(r["kind"] == "newer" and r["version"] == "1.6", f"1.6 over 1.5.1: {r}")
check(r["url"].startswith("https://"), "newer: no release page")
check(ask(cfg, ("ok", release("1.5.1")))["kind"] == "current", "same version is not 'current'")
check(ask(cfg, ("ok", release("v1.5")))["kind"] == "current", "an older release is not 'current'")
check(ask(cfg, ("ok", release("1.10")), "1.9")["kind"] == "newer", "1.10 is not newer than 1.9")
check(ask(cfg, ("ok", release("1.5.2")))["kind"] == "newer", "1.5.2 is not newer than 1.5.1")
check(ask(cfg, ("ok", release("1.5")), "1.5.1")["kind"] == "current", "1.5 over 1.5.1")
check(len(calls) == 6 and all(c["url"] == updatecheck.url(cfg) for c in calls),
      "one request per question, to the configured address")
check(calls[0]["ua"], "no user agent")

# --- 2. every kind of failure is a result, never an exception -------------------
for label, step in (("no network", ("net", "down")), ("403", ("http", 403)),
                    ("429", ("http", 429)), ("404", ("http", 404)),
                    ("broken JSON", ("ok", b"{nope")), ("not an object", ("ok", b"[1]")),
                    ("no tag", ("ok", json.dumps({"html_url": "https://x/"}).encode())),
                    ("odd tag", ("ok", release("nightly"))),
                    ("no page", ("ok", json.dumps({"tag_name": "9.9"}).encode())),
                    ("http page", ("ok", release("9.9", "http://x/")))):
    r = ask(cfg, step)
    check(r["kind"] == "failed" and r["message"], f"{label}: {r}")
check(ask(cfg, ("http", 404))["message"] == "no release is published yet",
      "a 404 is not explained as a missing release")

# --- 3. what is kept, and the notice -------------------------------------------
cfg, conn = make()
check(updatecheck.available(conn, "1.5.1") is None, "a notice with nothing found")
newer = ask(cfg, ("ok", release("1.6", "https://github.com/example/repo/releases/tag/1.6")))
updatecheck.finish(conn, newer, "auto", T0)
check(updatecheck.available(conn, "1.5.1") == ("1.6", "https://github.com/example/repo/releases/tag/1.6"),
      "the newer release is not kept")
check(updatecheck.available(conn, "1.6") is None, "a notice for the version you have")
check(updatecheck.available(conn, "1.7") is None, "a notice for an older release")
failed = ask(cfg, ("net", "down"))
updatecheck.finish(conn, failed, "manual", T0 + timedelta(hours=25))
check(updatecheck.available(conn, "1.5.1") is not None, "a failure cleared the notice")
current = ask(cfg, ("ok", release("1.5.1")))
updatecheck.finish(conn, current, "manual", T0 + timedelta(hours=26))
check(updatecheck.available(conn, "1.5.1") is None, "up to date did not clear the notice")
check(updatecheck.last(conn)["detail"].startswith("up to date"), "the log row says nothing")

# --- 4. the schedule: one in 24 hours, a failure waits a day, the button counts ----
cfg, conn = make()
check(updatecheck.is_due(conn, T0), "never asked: not due")
updatecheck.finish(conn, ask(cfg, ("ok", release("1.5.1"))), "auto", T0)
check(not updatecheck.is_due(conn, T0 + timedelta(hours=23)), "due after 23 h")
check(updatecheck.is_due(conn, T0 + timedelta(hours=24)), "not due after 24 h")
updatecheck.finish(conn, ask(cfg, ("http", 403)), "manual", T0 + timedelta(hours=30))
check(not updatecheck.is_due(conn, T0 + timedelta(hours=40)),
      "the button did not hold the automatic one off")
check(updatecheck.is_due(conn, T0 + timedelta(hours=54)), "not due a day after the button")
cfg, conn = make()
updatecheck.finish(conn, ask(cfg, ("http", 429)), "auto", T0)
check(not updatecheck.is_due(conn, T0 + timedelta(hours=1)), "retried within the hour after a 429")
check(updatecheck.is_due(conn, T0 + timedelta(hours=24)), "not due 24 h after a 429")
# The other jobs' rows do not count.
cfg, conn = make()
fetch.record(conn, T0, "auto", 200, "x", "profile")
check(updatecheck.is_due(conn, T0 + timedelta(hours=1)), "a profile row held the check off")

# --- 5. the switch, the button, and the object the clock asks -----------------------
from PySide6.QtCore import QCoreApplication  # noqa: E402

app = QCoreApplication.instance() or QCoreApplication([])
from ui.workers.update_check import UpdateChecker  # noqa: E402

cfg, conn = make(auto=False)
box = UpdateChecker(cfg, clock=lambda: T0)
check(not box.due(T0), "switch off: an automatic check is due")
cfg.override("updates.auto_check", True)
check(box.due(T0), "switch on, never asked: not due")
cfg.override("updates.url", "")
check(not box.due(T0), "no address: due")
check(not box.start("manual"), "no address: the button started a request")
cfg.override("updates.url", "https://api.example.invalid/releases/latest")
cfg.override("updates.auto_check", False)
check(not box.due(T0 + timedelta(days=9)), "switch off: due later on")
before = len(calls)
script.append(("ok", release("9.0")))
seen = []
box.finished.connect(seen.append)
check(box.start("manual"), "switch off: the button did not start")
check(not box.start("manual"), "a second request started while one runs")
box._worker.wait()
for _ in range(20):
    app.processEvents()
check(len(calls) == before + 1, "the button made other than one request")
check(seen and seen[0]["kind"] == "newer" and seen[0]["manual"] is True,
      f"the button's answer: {seen}")
conn2 = dbq.connect(cfg.db_path)
row = updatecheck.last(conn2)
check(row["kind"] == "manual" and row["status"] == 200, f"the button's log row: {dict(row)}")
check(updatecheck.available(conn2, "1.5.1") == ("9.0", "https://github.com/example/repo/releases/tag/x"),
      "the button's answer left no notice")
cfg.override("updates.auto_check", True)
box.clock = lambda: T0 + timedelta(hours=1)
check(not box.due(), "due within an hour of the button")

print("update check       : releases compared, failures quiet, schedule and the button")
if problems:
    print("FAILED -", len(problems), "problem(s)")
    for line in problems:
        print("  ", line)
    sys.exit(1)
print("OK")
