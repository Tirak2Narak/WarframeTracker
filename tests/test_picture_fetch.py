#!/usr/bin/env python3
"""The picture fetch (wft/data/pictures.py), without Qt.

`pictures._fetch` is replaced for the whole run and the pause is set to 0:
nothing here reaches a server. Each case counts the calls, because "how often,
and never against a refusal" is the rule this feature lives by.

Run: python3 tests/test_picture_fetch.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="wft-config-")
# Even a config that forgot to name its data folder lands in a scratch one.
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="wft-data-")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from wft import config as config_mod  # noqa: E402
from wft.data import db as dbq  # noqa: E402
from wft.data import pictures  # noqa: E402
from wft.profile import fetch  # noqa: E402

problems: list[str] = []


def check(ok, text):
    if not ok:
        problems.append(text)


T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
PNG = cv2.imencode(".png", np.full((8, 8, 3), 200, np.uint8))[1].tobytes()

pictures.PAUSE = 0
calls: list[str] = []
#: What the calls answer, by location; a missing key answers with a picture.
script: dict = {}


def fake_fetch(url):
    location = url[len(pictures.CONTENT_BASE):]
    calls.append(location)
    step = script.get(location, "ok")
    if step == "ok":
        return PNG
    if step == "junk":
        return b"not an image"
    if step == "net":
        raise urllib.error.URLError("no route")
    if step == "timeout":
        raise TimeoutError("timed out")
    raise urllib.error.HTTPError(url, int(step), "x", {}, None)


pictures._fetch = fake_fetch


def make(count=5, *, auto=True):
    """A database holding `count` items with an icon on record and no picture."""
    directory = pathlib.Path(tempfile.mkdtemp())
    cfg = config_mod.load()
    cfg.override("paths.data_dir", str(directory))
    cfg.override("pictures.auto_fetch", auto)
    conn = dbq.connect(cfg.db_path)
    for n in range(count):
        unique = f"/I/{n}"
        conn.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                     "VALUES(?,?,?,0)", (unique, f"Item{n}", "warframe"))
        conn.execute("INSERT INTO owned_items(item_unique, count) VALUES(?,1)", (unique,))
        conn.execute("INSERT INTO textures(unique_name, location) VALUES(?,?)",
                     (unique, f"/img/{n}.png"))
    conn.commit()
    return cfg, conn


def run(conn, at, kind="auto"):
    """What the card's fetcher does, in one place: begin, fetch, end."""
    row = pictures.begin(conn, at, kind)
    if row is None:
        return None, None
    counts = pictures.fetch_missing(conn)
    return row, pictures.end(conn, row, counts)


# --- 1. a plain run ------------------------------------------------------------
cfg, conn = make(5)
calls.clear()
counts = pictures.fetch_missing(conn)
check(counts["fetched"] == 5 and counts["gone"] == 0 and counts["stopped"] is None
      and not counts["cancelled"] and len(calls) == 5, f"plain run: {counts} {len(calls)}")
check(dbq.thumbnail_count(conn) == 5, "plain run: pictures not stored")
calls.clear()
counts = pictures.fetch_missing(conn)
check(not calls and counts["pending"] == 0, f"second run asked again: {len(calls)}")

# --- 2. a 404 is skipped, the run goes on ----------------------------------------
cfg, conn = make(5)
script.clear()
script["/img/1.png"] = "404"
calls.clear()
counts = pictures.fetch_missing(conn)
check(counts["fetched"] == 4 and counts["gone"] == 1 and counts["stopped"] is None
      and len(calls) == 5, f"404: {counts} {len(calls)}")
check(pictures.summary(counts) == "4 fetched, 1 missing on the server",
      pictures.summary(counts))

# --- 3. any other failure ends the run at once --------------------------------------
for step, reason, status in (("503", "HTTP 503", 503), ("403", "HTTP 403", 403),
                             ("429", "HTTP 429", 429), ("net", "URLError: no route", None),
                             ("timeout", "TimeoutError: timed out", None)):
    cfg, conn = make(5)
    script.clear()
    script["/img/2.png"] = step
    calls.clear()
    counts = pictures.fetch_missing(conn)
    check(len(calls) == 3, f"{step}: {len(calls)} call(s), wanted 3 (2 pictures, the failing one)")
    check(counts["fetched"] == 2 and counts["stopped"] == reason
          and counts["stop_status"] == status, f"{step}: {counts}")
    check(dbq.thumbnail_count(conn) == 2, f"{step}: stored {dbq.thumbnail_count(conn)}")
    check(pictures.summary(counts) == f"2 fetched; stopped: {reason}", pictures.summary(counts))

# --- 4. unreadable and cancelled -------------------------------------------------
cfg, conn = make(4)
script.clear()
script["/img/0.png"] = "junk"
counts = pictures.fetch_missing(conn)
check(counts["failed"] == 1 and counts["fetched"] == 3, f"junk: {counts}")
cfg, conn = make(4)
script.clear()
calls.clear()
counts = pictures.fetch_missing(conn, cancel=lambda: len(calls) >= 2)
check(counts["cancelled"] and counts["stopped"] is None and len(calls) == 2
      and counts["fetched"] == 2, f"cancel: {counts} {len(calls)}")
check("stopped by you" in pictures.summary(counts), pictures.summary(counts))

# --- 5. nothing missing: no call, no log ---------------------------------------------
cfg, conn = make(0)
calls.clear()
row, text = run(conn, T0)
check(row is None and not calls, "nothing missing: a run began")
check(fetch.last(conn, pictures.JOB) is None, "nothing missing: a row was logged")

# --- 6. the log: written at the start, filled at the end -------------------------------
cfg, conn = make(3)
script.clear()
row = pictures.begin(conn, T0, "import")
logged = fetch.last(conn, pictures.JOB)
check(row is not None and logged["id"] == row and logged["kind"] == "import"
      and logged["status"] is None and logged["detail"] == "no answer recorded",
      f"log at the start: {dict(logged) if logged else None}")
check(not pictures.is_due(conn, T0 + timedelta(hours=5)),
      "a run that has begun counts for the schedule at once")
counts = pictures.fetch_missing(conn)
text = pictures.end(conn, row, counts)
logged = fetch.last(conn, pictures.JOB)
check(text == "3 fetched" and logged["detail"] == "3 fetched" and logged["status"] is None,
      f"log at the end: {text!r} {dict(logged)}")
check(conn.execute("SELECT COUNT(*) FROM fetch_log WHERE job='pictures'").fetchone()[0] == 1,
      "one run, more than one row")
check(fetch.last(conn) is None, "the picture run wrote a profile row")

# a stopped run keeps its code
cfg, conn = make(3)
script.clear()
script["/img/1.png"] = "503"
row, text = run(conn, T0)
logged = fetch.last(conn, pictures.JOB)
check(logged["status"] == 503 and "stopped: HTTP 503" in logged["detail"], dict(logged))
# ...and holds off the run an applied import would start, for the day; a run
# that ended by itself or by the user's Stop does not.
check(pictures.refused_recently(conn, T0 + timedelta(hours=23)),
      "23 h after a refusal: an import run would ask again")
check(not pictures.refused_recently(conn, T0 + timedelta(hours=25)),
      "25 h after a refusal: an import run is still held off")
for counts_end in ({"fetched": 3}, {"fetched": 1, "cancelled": True}):
    cfg, conn = make(3)
    row = pictures.begin(conn, T0, "auto")
    pictures.end(conn, row, counts_end)
    check(not pictures.refused_recently(conn, T0 + timedelta(hours=1)),
          f"a run ending {pictures.summary(counts_end)!r} counts as a refusal")

# --- 7. the schedule on a made-up clock ------------------------------------------------
def due_at(conn, cfg, now):
    return pictures.enabled(cfg) and pictures.is_due(conn, now)


cfg, conn = make(3)
check(due_at(conn, cfg, T0), "never run: not due")
script.clear()
run(conn, T0, "auto")
check(not due_at(conn, cfg, T0 + timedelta(hours=23)), "due 23 h after a run")
check(due_at(conn, cfg, T0 + timedelta(hours=25)), "not due 25 h after a run")
# a run of any kind moves the schedule
conn.execute("INSERT INTO items(unique_name, name, category, is_prime) VALUES('/I/x','x','warframe',0)")
conn.execute("INSERT INTO owned_items(item_unique, count) VALUES('/I/x',1)")
conn.execute("INSERT INTO textures VALUES('/I/x','/img/x.png')")
conn.commit()
run(conn, T0 + timedelta(hours=30), "import")
check(not due_at(conn, cfg, T0 + timedelta(hours=52)), "22 h after an import run: due")
check(due_at(conn, cfg, T0 + timedelta(hours=55)), "25 h after an import run: not due")
# a failed run waits the day too
conn.execute("INSERT INTO items(unique_name, name, category, is_prime) VALUES('/I/y','y','warframe',0)")
conn.execute("INSERT INTO owned_items(item_unique, count) VALUES('/I/y',1)")
conn.execute("INSERT INTO textures VALUES('/I/y','/img/y.png')")
conn.commit()
script["/img/y.png"] = "503"
calls.clear()
run(conn, T0 + timedelta(hours=60), "auto")
check(not due_at(conn, cfg, T0 + timedelta(hours=83)), "23 h after a failed run: due")
check(due_at(conn, cfg, T0 + timedelta(hours=85)), "25 h after a failed run: not due")
# the switch
cfg.override("pictures.auto_fetch", False)
check(not due_at(conn, cfg, T0 + timedelta(hours=500)), "switch off: due")
cfg.override("pictures.auto_fetch", True)
# a connection of its own (a restart) knows it all
conn2 = dbq.connect(cfg.db_path)
check(not pictures.is_due(conn2, T0 + timedelta(hours=83))
      and pictures.is_due(conn2, T0 + timedelta(hours=85)), "a new connection does not know the log")

# --- 8. wf doctor ------------------------------------------------------------------------
from wft import doctor  # noqa: E402

cfg, conn = make(2)
line = doctor._picture_fetch_line(cfg)
check(line == "on; never run", f"doctor, never run: {line!r}")
script.clear()
run(conn, T0, "auto")
line = doctor._picture_fetch_line(cfg)
check(line.startswith("on; last 2026-10-24 12:00:00 UTC (auto): 2 fetched"), line)
cfg.override("pictures.auto_fetch", False)
check(doctor._picture_fetch_line(cfg).startswith("off (switched off)"), "doctor, switch off")

print("picture fetch      :", "OK" if not problems else f"{len(problems)} problem(s)")
for text in problems:
    print("   ", text)
sys.exit(1 if problems else 0)
