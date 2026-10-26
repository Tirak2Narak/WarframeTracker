#!/usr/bin/env python3
"""The daily profile fetch (wft/profile/fetch.py), without Qt.

`urllib.request.urlopen` is replaced for the whole run: nothing here reaches a
server, and each case counts how often the request was made, because "exactly
once, never again" is the rule this feature lives by.

Run: python3 tests/test_profile_fetch.py
"""
from __future__ import annotations

import email.utils
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="wft-config-")
# Even a config that forgot to name its data folder lands in a scratch one.
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="wft-data-")

from wft import config as config_mod  # noqa: E402
from wft.data import db as dbq  # noqa: E402
from wft.data.publicexport import USER_AGENT  # noqa: E402
from wft.profile import fetch, mastery  # noqa: E402

problems: list[str] = []


def check(ok, text):
    if not ok:
        problems.append(text)


ID = "0123456789abcdef01234567"
T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

calls: list[dict] = []
#: What the next calls answer: ("ok", body, headers) | ("http", code) | ("net", text).
script: list = []


class _Response:
    def __init__(self, body, headers):
        self.status = 200
        self._body = body
        self.headers = headers

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
    return _Response(step[1], step[2])


urllib.request.urlopen = fake_urlopen


def profile_body(*pairs):
    xp = [{"ItemType": u, "XP": x} for u, x in pairs]
    return json.dumps({"Results": [{"LoadOutInventory": {"XPInfo": xp}}]}).encode()


def headers(date: datetime, age: int = 0):
    return {"Date": email.utils.format_datetime(date, usegmt=True), "Age": str(age)}


def make(*, auto=True, account=ID):
    directory = pathlib.Path(tempfile.mkdtemp())
    cfg = config_mod.load()
    cfg.override("paths.data_dir", str(directory))
    cfg.override("profile.account_id", account)
    cfg.override("profile.auto_fetch", auto)
    conn = dbq.connect(cfg.db_path)
    for unique, name in (("/F/Ash", "Ash"), ("/F/Bo", "Bo"), ("/F/Cy", "Cy"),
                         ("/F/Di", "Di"), ("/F/Ed", "Ed")):
        conn.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                     "VALUES(?,?,?,0)", (unique, name, "warframe"))
    conn.commit()
    return cfg, conn


# --- 1. the schedule, on a made-up clock ------------------------------------------
cfg, conn = make()
check(fetch.is_due(conn, T0), "never fetched: not due")
fetch.record(conn, T0, "auto", 200, "x")
check(not fetch.is_due(conn, T0 + timedelta(hours=23)), "23 h after: due")
check(fetch.is_due(conn, T0 + timedelta(hours=25)), "25 h after: not due")
check(fetch.next_due(conn) == T0 + timedelta(hours=24), "next_due is not +24 h")
# A reload counts: the next automatic one is 24 h after the reload.
fetch.record(conn, T0 + timedelta(hours=20), "manual", 200, "x")
check(not fetch.is_due(conn, T0 + timedelta(hours=25)), "after a reload: due at +25 h")
check(fetch.is_due(conn, T0 + timedelta(hours=44, minutes=1)),
      "after a reload: not due at +44 h")
# A failure waits a day too, whoever made it.
cfg, conn = make()
fetch.record(conn, T0, "auto", 409, "HTTP 409")
check(not fetch.is_due(conn, T0 + timedelta(hours=1)), "after a 409: due again within the hour")
check(fetch.is_due(conn, T0 + timedelta(hours=24)), "after a 409: not due after 24 h")
fetch.record(conn, T0, "manual", None, "URLError: down")
check(fetch.last(conn)["status"] is None, "a failure without a status is not stored as NULL")
# A request is logged when it begins: one that never comes back (a crash, a
# quit) still holds the next automatic one off, and its answer fills the row
# in rather than adding a second one.
cfg, conn = make()
row = fetch.record(conn, T0, "auto", None, "no answer recorded")
check(not fetch.is_due(conn, T0 + timedelta(hours=1)),
      "a request without an answer yet: due again within the hour")
fetch.finish(conn, fetch.Outcome("failed", 409, "HTTP 409"), "auto", T0, row)
logged = conn.execute("SELECT status, detail FROM fetch_log").fetchall()
check([tuple(r) for r in logged] == [(409, "HTTP 409")],
      f"the answer did not fill in the row logged at the start: {logged!r}")

# --- 2. when nothing may happen ---------------------------------------------------
for label, kwargs, want in (("no id", {"account": ""}, False),
                            ("short id", {"account": "abc"}, False),
                            ("not hex", {"account": "g" * 24}, False),
                            ("switch off", {"auto": False}, False),
                            ("id and switch", {}, True)):
    c, _ = make(**kwargs)
    check(fetch.enabled(c) == want, f"enabled() for {label}: not {want}")

# --- 3. one request, exactly the specified one ------------------------------------
cfg, conn = make()
temp_before = set(pathlib.Path(tempfile.gettempdir()).glob("profile.json*"))
calls.clear()
script[:] = [("ok", profile_body(("/F/Ash", 900_000)), headers(T0, 100))]
out = fetch.fetch_profile(cfg, ID.upper(), None, T0)
check(len(calls) == 1, f"one fetch made {len(calls)} request(s)")
check(calls[0]["url"] == mastery.profile_url(ID), "wrong address")
check(calls[0]["ua"] == USER_AGENT, f"user agent {calls[0]['ua']!r}")
check(calls[0]["timeout"] == 30, f"timeout {calls[0]['timeout']!r}")
check(out.kind == "ok" and out.snapshot == T0 - timedelta(seconds=100),
      f"outcome {out.kind} {out.snapshot}")
path = fetch.profile_path(cfg)
check(path.is_file() and path.parent == cfg.data_dir, "profile.json not in the data folder")
leftovers = [p.name for p in cfg.data_dir.iterdir() if p.name.startswith("profile.json.")]
check(not leftovers, f"temporary files left behind: {leftovers}")
check(set(pathlib.Path(tempfile.gettempdir()).glob("profile.json*")) == temp_before,
      "profile.json written into the system temp folder")

# --- 4. an older or equal copy changes nothing ------------------------------------
before = path.read_bytes()
for label, shift in (("equal", 0), ("older", -300)):
    calls.clear()
    script[:] = [("ok", profile_body(("/F/Bo", 900_000)),
                  headers(T0 + timedelta(seconds=shift), 100))]
    out = fetch.fetch_profile(cfg, ID, T0 - timedelta(seconds=100), T0)
    check(out.kind == "stale", f"{label} copy: {out.kind}")
    check(path.read_bytes() == before, f"{label} copy replaced the kept file")
    check(len(calls) == 1, f"{label} copy: {len(calls)} request(s)")
script[:] = [("ok", profile_body(("/F/Bo", 900_000)),
              headers(T0 + timedelta(seconds=1), 100))]
out = fetch.fetch_profile(cfg, ID, T0 - timedelta(seconds=100), T0)
check(out.kind == "ok" and path.read_bytes() != before, "a newer copy was not kept")
# No Age / no Date: the snapshot falls back to "now".
check(fetch.snapshot_time({}, T0) == T0, "no headers: snapshot is not now")
check(fetch.snapshot_time({"age": "60"}, T0) == T0 - timedelta(seconds=60),
      "no Date: now minus Age")

# --- 5. a refusal is shown, never repeated ----------------------------------------
cfg, conn = make()
calls.clear()
script[:] = [("http", 409)]
out = fetch.fetch_profile(cfg, ID, None, T0)
check(out.kind == "failed" and out.status == 409, f"409: {out.kind} {out.status}")
check(len(calls) == 1, f"409: {len(calls)} request(s)")
check(not fetch.profile_path(cfg).exists(), "409 left a file")
script[:] = [("net", "no route")]
out = fetch.fetch_profile(cfg, ID, None, T0)
check(out.kind == "failed" and out.status is None and "no route" in out.detail,
      f"network error: {out.kind} {out.status} {out.detail!r}")
script[:] = [("ok", b"<html>not json</html>", headers(T0))]
out = fetch.fetch_profile(cfg, ID, None, T0)
check(out.kind == "failed" and out.status == 200 and not fetch.profile_path(cfg).exists(),
      "200 with no profile in it was accepted")
script[:] = [("ok", json.dumps({"Results": [{}]}).encode(), headers(T0))]
check(fetch.fetch_profile(cfg, ID, None, T0).kind == "failed",
      "200 without XPInfo was accepted")
check(not script, "an unused scripted answer is left over")

# --- 6. applying: the first time asks, later ones do not --------------------------
cfg, conn = make()
body1 = profile_body(("/F/Ash", 900_000), ("/F/Bo", 900_000), ("/F/Cy", 1000))
out = fetch.Outcome("ok", 200, "", T0, mastery.parse(body1))
res = fetch.finish(conn, out, "auto", T0)
rows = conn.execute("SELECT * FROM owned_items").fetchall()
check(res["kind"] == "review" and not rows,
      f"first profile: {res['kind']}, {len(rows)} row(s) written")
check(conn.execute("SELECT value FROM meta WHERE key='mastery_imported_at'").fetchone() is None,
      "first profile stamped the import")
check(conn.execute("SELECT value FROM meta WHERE key=?",
                   (fetch.SNAPSHOT_KEY,)).fetchone() is not None,
      "snapshot not remembered")
check(fetch.last(conn)["status"] == 200 and fetch.last(conn)["kind"] == "auto",
      "request not logged")
check(fetch.pending_review(conn, cfg) is None, "pending_review without a kept file")
fetch.save_atomically(fetch.profile_path(cfg), body1)
pending = fetch.pending_review(conn, cfg)
check(pending is not None and len(pending["mastered"]) == 2,
      "pending_review does not find the 2 items")
# Confirmed (as the dialog does): stamp. Now later profiles apply by themselves.
dbq.note_mastery_import(conn)
check(fetch.pending_review(conn, cfg) is None, "review still offered after the stamp")
body2 = profile_body(("/F/Ash", 900_000), ("/F/Di", 900_000), ("/F/Ed", 900_000))
out = fetch.Outcome("ok", 200, "", T0 + timedelta(days=1), mastery.parse(body2))
res = fetch.finish(conn, out, "manual", T0 + timedelta(days=1))
got = {r["item_unique"]: r["source"] for r in conn.execute(
    "SELECT item_unique, source FROM owned_items WHERE mastered = 1")}
check(res["kind"] == "applied" and set(got) == {"/F/Ash", "/F/Di", "/F/Ed"},
      f"second profile: {res['kind']} {sorted(got)}")
check(all(v == "profile" for v in got.values()), f"source {got}")
check("3 newly mastered" in res["message"] and "Di" in res["message"], res["message"])
# The same again: nothing new.
res = fetch.finish(conn, fetch.Outcome("ok", 200, "", T0 + timedelta(days=2),
                                       mastery.parse(body2)),
                   "auto", T0 + timedelta(days=2))
check(res["kind"] == "nothing", f"same profile again: {res['kind']}")
# "Applied before" also by a profile row alone.
cfg2, conn2 = make()
conn2.execute("INSERT INTO owned_items(item_unique,count,mastered,source) "
              "VALUES('/F/Ash',0,1,'profile')")
conn2.commit()
check(fetch.applied_before(conn2), "a profile row does not count as applied before")
conn2.execute("UPDATE owned_items SET source='manual'")
check(not fetch.applied_before(conn2), "a manual row counts as applied before")
found = {"mastered": [("u", n, 1) for n in "ABCDE"]}
check(fetch.names_line(found) == "A, B, C (+2 more)", fetch.names_line(found))

# --- 7. the warning count ---------------------------------------------------------
cfg, conn = make()
for hours in (1, 2, 3, 4, 30):
    fetch.record(conn, T0 - timedelta(hours=hours), "manual", 200, "x")
check(fetch.count_recent(conn, T0) == 4,
      f"4 inside 24 h, counted {fetch.count_recent(conn, T0)}")

# --- 8. an old database opens and gains the log -----------------------------------
cfg, conn = make()
conn.execute("DROP TABLE fetch_log")
conn.commit()
conn.close()
conn = dbq.connect(cfg.db_path)
try:
    fetch.record(conn, T0, "auto", 200, "x")
    ok = fetch.last(conn) is not None
except sqlite3.Error:
    ok = False
check(ok, "a database without fetch_log did not get one")

# --- 9. wf doctor ------------------------------------------------------------------
from wft import doctor  # noqa: E402

cfg, conn = make()
line = doctor._profile_fetch_line(cfg)
check(line.startswith("on; never fetched"), f"doctor, never fetched: {line!r}")
fetch.record(conn, datetime.now(timezone.utc), "auto", 409, "HTTP 409")
line = doctor._profile_fetch_line(cfg)
check("on;" in line and "(auto) HTTP 409" in line and "1 request(s) in 24 h" in line,
      f"doctor, after a 409: {line!r}")
# The doctor writes nothing: on a database from before fetch_log it reports
# "never fetched" and leaves the table out rather than adding it.
cfg_old, conn_old = make()
conn_old.execute("DROP TABLE fetch_log")
conn_old.commit()
line = doctor._profile_fetch_line(cfg_old)
has = conn_old.execute("SELECT 1 FROM sqlite_master WHERE name = 'fetch_log'").fetchone()
check(line.startswith("on; never fetched") and has is None,
      f"doctor on an old database: {line!r}, table added: {has is not None}")
cfg.override("profile.auto_fetch", False)
check(doctor._profile_fetch_line(cfg).startswith("off (switched off)"), "doctor, switch off")
cfg.override("profile.account_id", "")
check(doctor._profile_fetch_line(cfg).startswith("off (no valid account id)"), "doctor, no id")

# --- Beasts count towards mastery and stay out of a scan -------------------
cfg, conn = make()
for unique, name in (("/B/Kubrow", "Test Kubrow"), ("/B/Kavat", "Test Kavat")):
    conn.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                 "VALUES(?,?,?,0)", (unique, name, "beast"))
conn.commit()
before = dbq.mastered_count(conn)
check(before == (0, 7), f"beasts in the denominator: {before!r}")
found = mastery.resolve(conn, [
    {"ItemType": "/B/Kubrow", "XP": 904_258},       # at the 900,000 cap
    {"ItemType": "/B/Kavat", "XP": 600_000}])       # not yet
check([m[0] for m in found["mastered"]] == ["/B/Kubrow"] and found["short"] == 1,
      f"a beast in a profile is marked at its cap only: {found!r}")
check(mastery.threshold("Test Kubrow", "beast") == mastery.threshold("Ash", "companion"),
      "a beast takes the companion threshold")
dbq.set_item_mastered(conn, "/B/Kubrow", source="profile")
check(dbq.mastered_count(conn) == (1, 7),
      f"mastered_count counts a mastered beast: {dbq.mastered_count(conn)!r}")
from wft.inventory import scan as scan_mod  # noqa: E402
names = scan_mod._tracked_names(conn)
check("Test Kubrow" not in names and "Test Kavat" not in names,
      "a beast name read by OCR must stay unrecognised and ignored")
check("Ash" in names, "the scan lookup still knows tracked items")
check("beast" in dbq.NOT_IN_RECORDINGS and "beast" not in dbq.NOT_FOR_MASTERY_CATEGORIES,
      "beast: counted for mastery, never read in a recording")

print("profile fetch      :", "OK" if not problems else f"{len(problems)} problem(s)")
for text in problems:
    print("   ", text)
sys.exit(1 if problems else 0)
